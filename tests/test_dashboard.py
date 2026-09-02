"""Dashboard: server-side SVG rendering, the JSON snapshot and the HTTP layer."""

from __future__ import annotations

import json
import threading
import urllib.request
import xml.etree.ElementTree as ElementTree

import pytest

from factorylink.dashboard import (
    PAGE,
    DashboardApp,
    TrendSpec,
    render_sparkline,
    render_trend_svg,
    serve,
)
from factorylink.historian import Historian
from factorylink.runtime import build_simulated_runtime


# -- SVG rendering ---------------------------------------------------------


def test_trend_svg_is_well_formed_xml():
    """The page embeds this inline, so it has to parse."""
    svg = render_trend_svg([(float(i), i * 0.5) for i in range(40)], title="t", unit="A")
    root = ElementTree.fromstring(svg)
    assert root.tag.endswith("svg")
    assert root.attrib["width"] == "460"
    assert svg.count("<path") == 1


def test_trend_svg_scales_the_trace_into_the_plot_area():
    """Every point must land inside the chart box."""
    svg = render_trend_svg([(float(i), i * 100.0) for i in range(20)], width=400, height=200)
    root = ElementTree.fromstring(svg)
    path = [e for e in root if e.tag.endswith("path")][0]
    coordinates = [
        tuple(float(n) for n in token[1:].split(","))
        for token in path.attrib["d"].split()
    ]
    assert all(0 <= x <= 400 for x, _ in coordinates)
    assert all(0 <= y <= 200 for _, y in coordinates)


def test_trend_svg_draws_alarm_limit_lines():
    """Seeing the limit on the trend is half the value of the trend."""
    svg = render_trend_svg(
        [(float(i), 10.0 + i) for i in range(20)],
        limits=[("H", 20.0, "#e07b1a"), ("HH", 25.0, "#c0392b")],
    )
    assert svg.count("stroke-dasharray") == 2
    assert ">H<" in svg and ">HH<" in svg


def test_trend_svg_handles_too_little_data():
    """A tag with one archived point must not blow up the page."""
    svg = render_trend_svg([(0.0, 1.0)])
    assert "not enough history yet" in svg
    ElementTree.fromstring(svg)


def test_trend_svg_handles_a_flat_signal():
    """A constant signal has zero span; the y-axis must not divide by zero."""
    svg = render_trend_svg([(float(i), 5.0) for i in range(10)])
    ElementTree.fromstring(svg)
    assert "5" in svg


def test_trend_svg_escapes_its_title():
    """Titles come from a tag database that a customer edits."""
    svg = render_trend_svg([(0.0, 1.0), (1.0, 2.0)], title="a<b>&c")
    assert "<b>" not in svg
    assert "&amp;c" in svg
    ElementTree.fromstring(svg)


def test_sparkline_renders_and_degrades():
    """Tiny inline chart for the table."""
    assert "<path" in render_sparkline([1.0, 2.0, 1.5, 3.0])
    assert "<path" not in render_sparkline([1.0])
    ElementTree.fromstring(render_sparkline([1.0, 2.0, 3.0]))


# -- snapshot --------------------------------------------------------------


@pytest.fixture()
def app(clock):
    """A dashboard over a short simulated run, with a fault injected."""
    runtime, plc = build_simulated_runtime(clock=clock, seed=5)
    from factorylink.protocols.simulator import Fault

    plc.schedule_fault(60.0, Fault.JAM, 40.0)
    runtime.run(300.0)
    runtime.historian.flush()
    return DashboardApp(
        runtime.db,
        poller=runtime.poller,
        alarms=runtime.alarms,
        historian=runtime.historian,
        oee=runtime.oee,
        guard=runtime.guard,
    )


def test_snapshot_is_json_serialisable(app):
    """The API returns this verbatim."""
    payload = json.dumps(app.snapshot(), default=str)
    assert len(payload) > 1000
    assert json.loads(payload)["title"] == "factorylink"


def test_snapshot_contains_every_polled_tag(app):
    """The live table is driven from the poller's last values."""
    snapshot = app.snapshot()
    assert len(snapshot["tags"]) == len(app.db)
    row = next(r for r in snapshot["tags"] if r["name"] == "tank_level")
    assert row["unit"] == "%"
    assert row["quality"] == "good"
    assert row["address"] == "holding:6"
    assert row["display"].endswith("%")


def test_snapshot_contains_alarms_and_oee(app):
    """Banner and OEE panel both come from one snapshot call."""
    snapshot = app.snapshot()
    assert snapshot["alarm_summary"]["configured"] > 0
    assert 0.0 <= snapshot["oee"]["availability"] <= 1.0
    assert 0.0 <= snapshot["oee"]["oee"] <= 1.0
    assert isinstance(snapshot["oee"]["pareto"], list)
    assert snapshot["safety"]["read_only"] is True


def test_snapshot_trends_are_rendered_server_side(app):
    """No chart library, no CDN: the SVG arrives finished."""
    snapshot = app.snapshot()
    assert len(snapshot["trends"]) == 4
    for trend in snapshot["trends"]:
        assert trend["svg"].startswith("<svg")
        ElementTree.fromstring(trend["svg"])


def test_acknowledging_through_the_app(app):
    """The ack button posts here."""
    app.alarms.update({"tank_level": 1.0}, now=app.now())
    result = app.acknowledge("*", operator="tester")
    assert result["ok"] is True
    assert app.acknowledge("no_such_alarm")["ok"] is False


def test_a_dashboard_with_no_components_still_renders(db):
    """Every component is optional."""
    bare = DashboardApp(db, now=lambda: 0.0)
    snapshot = bare.snapshot()
    assert snapshot["tags"] == []
    assert snapshot["alarms"] == []
    assert snapshot["oee"] is None
    assert bare.acknowledge("x")["ok"] is False


def test_trend_spec_selects_which_tags_are_charted(db, clock):
    """The chart list is configuration, not hard-coded."""
    historian = Historian(clock=clock, compression=False)
    for i in range(50):
        historian.record("tank_level", 50.0 + i * 0.1, timestamp=float(i))
    app = DashboardApp(
        db, historian=historian, trends=[TrendSpec("tank_level")], now=lambda: 60.0
    )
    snapshot = app.snapshot()
    assert len(snapshot["trends"]) == 1
    assert snapshot["trends"][0]["tag"] == "tank_level"
    assert "not enough history" not in snapshot["trends"][0]["svg"]


# -- HTTP ------------------------------------------------------------------


def fetch(port: int, path: str) -> tuple[int, str]:
    """GET one path from the local test server."""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
        return response.status, response.read().decode("utf-8")


@pytest.fixture()
def server(app):
    """A live server on an ephemeral loopback port."""
    httpd = serve(app, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutting_down = True
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def test_index_serves_a_self_contained_page(server):
    """No build step and no external resources at all."""
    status, body = fetch(server.server_address[1], "/")
    assert status == 200
    assert "<title>factorylink</title>" in body
    assert "http://" not in body.replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert "cdn" not in body.lower()
    assert "<script src" not in body


def test_state_endpoint_returns_the_snapshot(server):
    """The polling fallback and the SSE stream share this payload."""
    status, body = fetch(server.server_address[1], "/api/state")
    payload = json.loads(body)
    assert status == 200
    assert payload["title"] == "factorylink"
    assert len(payload["tags"]) > 30


def test_svg_endpoint_returns_a_single_chart(server):
    """Useful for embedding one trend in another page or a report."""
    status, body = fetch(server.server_address[1], "/svg/trend?tag=tank_level&span=300")
    assert status == 200
    assert body.startswith("<svg")
    ElementTree.fromstring(body)


def test_health_and_404(server):
    """A health endpoint for a supervisor, and honest 404s."""
    assert fetch(server.server_address[1], "/api/health")[0] == 200
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        fetch(server.server_address[1], "/nope")
    assert excinfo.value.code == 404


def test_ack_endpoint_acknowledges(server, app):
    """POST /api/ack is what the buttons call."""
    port = server.server_address[1]
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/ack",
        data=json.dumps({"alarm": "*", "operator": "tester"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert payload["ok"] is True


def test_the_page_constant_has_no_external_references():
    """Pinned separately from the server so a regression is obvious."""
    assert "cdnjs" not in PAGE
    assert "unpkg" not in PAGE
    assert "googleapis" not in PAGE
    assert PAGE.count("<script") == 1
