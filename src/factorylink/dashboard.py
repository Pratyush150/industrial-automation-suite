"""A zero-dependency live dashboard: stdlib HTTP server, server-rendered SVG.

There is no build step, no npm, no CDN and no chart library. The page is one
HTML string; the trends are SVG generated on the server from the historian.
That matters on a plant floor, where the machine running this is often a small
industrial PC on a network segment with no route to the internet, and where
"just run npm install" is not an available move.

Layout:

    +--------------------------------------------------------------+
    | alarm banner (worst active alarm, colour-coded)               |
    +---------------------------+----------------------------------+
    | live tag table            | OEE panel: A / P / Q / composite |
    | (value, unit, quality)    | + downtime Pareto                |
    +---------------------------+----------------------------------+
    | trend charts, inline SVG, rendered server-side                |
    +--------------------------------------------------------------+
    | alarm list with acknowledge buttons                           |
    +--------------------------------------------------------------+

Updates arrive over Server-Sent Events when the browser supports them, with a
plain polling fallback. Both paths hit the same ``/api/state`` snapshot, so
there is one code path to keep correct.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Sequence
from urllib.parse import parse_qs, urlparse

from .alarms import AlarmEngine, Severity
from .historian import Historian, Sample
from .oee import OEECalculator, OEEResult
from .poller import Poller
from .safety import WriteGuard
from .tags import TagDatabase

__all__ = [
    "render_trend_svg",
    "render_sparkline",
    "DashboardApp",
    "make_handler",
    "serve",
    "PAGE",
]

SEVERITY_COLOUR = {
    Severity.CRITICAL: "#c0392b",
    Severity.HIGH: "#e07b1a",
    Severity.MEDIUM: "#c9a227",
    Severity.LOW: "#4b7bec",
    Severity.DIAGNOSTIC: "#7f8c8d",
}


def _escape(text: Any) -> str:
    """Minimal XML escaping for text placed inside SVG."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_trend_svg(
    points: Sequence[tuple[float, float]],
    width: int = 460,
    height: int = 170,
    title: str = "",
    unit: str = "",
    colour: str = "#2d7ff9",
    limits: Sequence[tuple[str, float, str]] = (),
) -> str:
    """Render a time series as a standalone inline SVG string.

    Args:
        points: ``(timestamp, value)`` pairs, any order.
        width, height: pixel size of the chart.
        title: chart heading.
        unit: engineering unit shown on the y-axis labels.
        colour: stroke colour of the trace.
        limits: ``(label, value, colour)`` horizontal reference lines, used to
            draw alarm limits onto the trend.

    Returns:
        A complete ``<svg>...</svg>`` element. Pure function -- no historian,
        no server, so it is directly unit-testable.
    """
    pad_left, pad_right, pad_top, pad_bottom = 52, 12, 24, 22
    plot_w = max(1, width - pad_left - pad_right)
    plot_h = max(1, height - pad_top - pad_bottom)
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" class="trend" role="img" '
        f'aria-label="{_escape(title)} trend">'
    ]
    parts.append(
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff" '
        f'stroke="#d8dee6"/>'
    )
    if title:
        parts.append(
            f'<text x="8" y="16" font-family="monospace" font-size="12" '
            f'fill="#1c2733">{_escape(title)}</text>'
        )

    ordered = sorted(points, key=lambda p: p[0])
    if len(ordered) < 2:
        parts.append(
            f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
            f'font-family="monospace" font-size="12" fill="#8a94a3">'
            f"not enough history yet</text></svg>"
        )
        return "".join(parts)

    xs = [p[0] for p in ordered]
    ys = [p[1] for p in ordered]
    extra = [value for _, value, _ in limits]
    y_min = min(ys + extra)
    y_max = max(ys + extra)
    if y_max - y_min < 1e-9:
        y_min -= 0.5
        y_max += 0.5
    span = y_max - y_min
    y_min -= span * 0.08
    y_max += span * 0.08
    span = y_max - y_min
    x_min, x_max = xs[0], xs[-1]
    x_span = (x_max - x_min) or 1.0

    def sx(value: float) -> float:
        return pad_left + (value - x_min) / x_span * plot_w

    def sy(value: float) -> float:
        return pad_top + (y_max - value) / span * plot_h

    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = pad_top + fraction * plot_h
        label = y_max - fraction * span
        parts.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{pad_left + plot_w}" '
            f'y2="{y:.1f}" stroke="#eef1f5"/>'
        )
        parts.append(
            f'<text x="{pad_left - 6}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-family="monospace" font-size="10" fill="#8a94a3">'
            f"{label:.5g}</text>"
        )

    for label, value, limit_colour in limits:
        if not y_min <= value <= y_max:
            continue
        y = sy(value)
        parts.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{pad_left + plot_w}" y2="{y:.1f}" '
            f'stroke="{limit_colour}" stroke-width="1" stroke-dasharray="4 3"/>'
        )
        parts.append(
            f'<text x="{pad_left + plot_w - 2}" y="{y - 3:.1f}" text-anchor="end" '
            f'font-family="monospace" font-size="9" fill="{limit_colour}">'
            f"{_escape(label)}</text>"
        )

    path = " ".join(
        f"{'M' if index == 0 else 'L'}{sx(t):.1f},{sy(v):.1f}"
        for index, (t, v) in enumerate(ordered)
    )
    parts.append(f'<path d="{path}" fill="none" stroke="{colour}" stroke-width="1.6"/>')
    last_t, last_v = ordered[-1]
    parts.append(f'<circle cx="{sx(last_t):.1f}" cy="{sy(last_v):.1f}" r="2.5" fill="{colour}"/>')
    parts.append(
        f'<text x="{width - pad_right}" y="{height - 6}" text-anchor="end" '
        f'font-family="monospace" font-size="10" fill="#5b6675">'
        f"{last_v:.4g} {_escape(unit)}</text>"
    )
    parts.append(
        f'<text x="{pad_left}" y="{height - 6}" font-family="monospace" font-size="10" '
        f'fill="#8a94a3">{x_max - x_min:.0f}s window, {len(ordered)} points</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def render_sparkline(values: Sequence[float], width: int = 90, height: int = 18) -> str:
    """Render a tiny inline sparkline for the live tag table."""
    if len(values) < 2:
        return f'<svg width="{width}" height="{height}"></svg>'
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    step = width / (len(values) - 1)
    path = " ".join(
        f"{'M' if i == 0 else 'L'}{i * step:.1f},{height - (v - low) / span * (height - 2) - 1:.1f}"
        for i, v in enumerate(values)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}"><path d="{path}" fill="none" '
        f'stroke="#2d7ff9" stroke-width="1.2"/></svg>'
    )


@dataclass
class TrendSpec:
    """One chart on the dashboard."""

    tag: str
    colour: str = "#2d7ff9"
    span: float = 300.0


class DashboardApp:
    """Assembles the JSON snapshot and the SVG trends from live components.

    Every component is optional: pass only a poller and you get a live tag
    table; add a historian and the trends appear; add an OEE calculator and
    the OEE panel appears.
    """

    def __init__(
        self,
        db: TagDatabase,
        poller: Poller | None = None,
        alarms: AlarmEngine | None = None,
        historian: Historian | None = None,
        oee: OEECalculator | None = None,
        guard: WriteGuard | None = None,
        trends: Sequence[TrendSpec] | None = None,
        title: str = "factorylink",
        now: Callable[[], float] | None = None,
    ) -> None:
        self.db = db
        self.poller = poller
        self.alarms = alarms
        self.historian = historian
        self.oee = oee
        self.guard = guard
        self.title = title
        self._now = now or (poller.clock.now if poller else __import__("time").time)
        self.trends = list(
            trends
            or [
                TrendSpec("conveyor_speed", "#2d7ff9"),
                TrendSpec("motor_current", "#e07b1a"),
                TrendSpec("tank_level", "#2e9e5b"),
                TrendSpec("fill_temperature", "#8e44ad"),
            ]
        )
        self._lock = threading.Lock()

    # -- data ---------------------------------------------------------------

    def now(self) -> float:
        """Current time according to the injected clock."""
        return self._now()

    def tag_rows(self) -> list[dict[str, Any]]:
        """One row per polled tag, formatted for the live table."""
        rows: list[dict[str, Any]] = []
        if self.poller is None:
            return rows
        for name, reading in self.poller.values.items():
            tag = self.db.get(name)
            if tag is None:
                continue
            rows.append(
                {
                    "name": name,
                    "value": reading.value,
                    "display": tag.format_value(reading.value),
                    "unit": tag.unit,
                    "quality": reading.quality.value,
                    "group": tag.poll_group,
                    "device": tag.device,
                    "address": f"{tag.area.value}:{tag.address}",
                    "timestamp": reading.timestamp,
                    "description": tag.description,
                }
            )
        rows.sort(key=lambda r: (r["group"], r["name"]))
        return rows

    def alarm_rows(self) -> list[dict[str, Any]]:
        """Alarms needing attention, worst first."""
        if self.alarms is None:
            return []
        return [inst.as_dict() for inst in self.alarms.annunciated()]

    def oee_panel(self) -> dict[str, Any] | None:
        """OEE factors plus the downtime Pareto, or None if not configured."""
        if self.oee is None:
            return None
        result: OEEResult = self.oee.result(self.now(), close_open_stops=False)
        return {
            **result.as_dict(),
            "pareto": [entry.as_dict() for entry in self.oee.pareto(top=6)],
        }

    def trend_points(self, tag: str, span: float) -> list[tuple[float, float]]:
        """Archived points for one tag over the last ``span`` seconds."""
        if self.historian is None:
            return []
        end = self.now()
        samples: list[Sample] = self.historian.query(tag, end - span, end + 1.0)
        return [(s.timestamp, s.value) for s in samples]

    def trend_svg(self, tag: str, span: float = 300.0, width: int = 460, height: int = 170) -> str:
        """Render one trend chart, with the tag's alarm limits overlaid."""
        definition = self.db.get(tag)
        unit = definition.unit if definition else ""
        limits: list[tuple[str, float, str]] = []
        if definition is not None:
            for label, value, colour in (
                ("HH", definition.alarm.hi_hi, "#c0392b"),
                ("H", definition.alarm.hi, "#e07b1a"),
                ("L", definition.alarm.lo, "#e07b1a"),
                ("LL", definition.alarm.lo_lo, "#c0392b"),
            ):
                if value is not None:
                    limits.append((label, float(value), colour))
        colour = next((t.colour for t in self.trends if t.tag == tag), "#2d7ff9")
        return render_trend_svg(
            self.trend_points(tag, span),
            width=width,
            height=height,
            title=tag,
            unit=unit,
            colour=colour,
            limits=limits,
        )

    def snapshot(self) -> dict[str, Any]:
        """Everything the page needs, in one JSON-serialisable dict."""
        with self._lock:
            payload: dict[str, Any] = {
                "title": self.title,
                "now": self.now(),
                "tags": self.tag_rows(),
                "alarms": self.alarm_rows(),
                "alarm_summary": self.alarms.summary() if self.alarms else None,
                "oee": self.oee_panel(),
                "poller": self.poller.snapshot() if self.poller else None,
                "safety": self.guard.status() if self.guard else None,
                "trends": [
                    {"tag": spec.tag, "svg": self.trend_svg(spec.tag, spec.span)}
                    for spec in self.trends
                    if spec.tag in self.db
                ],
            }
        return payload

    def acknowledge(self, name: str, operator: str = "web") -> dict[str, Any]:
        """Acknowledge one alarm, or all of them when ``name`` is ``*``."""
        if self.alarms is None:
            return {"ok": False, "error": "no alarm engine configured"}
        try:
            if name == "*":
                events = self.alarms.acknowledge_all(by=operator)
                return {"ok": True, "acknowledged": len(events)}
            self.alarms.acknowledge(name, by=operator)
            return {"ok": True, "acknowledged": 1}
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}


PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>factorylink</title>
<style>
:root{--bg:#f4f6f9;--panel:#fff;--line:#d8dee6;--ink:#1c2733;--dim:#5b6675;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{background:#1c2733;color:#fff;padding:10px 16px;display:flex;
       justify-content:space-between;align-items:center}
header h1{font-size:15px;margin:0;letter-spacing:.5px}
header .meta{color:#9fb0c4;font-size:11px}
#banner{padding:10px 16px;font-weight:bold;color:#fff;display:none}
main{display:grid;grid-template-columns:2fr 1fr;gap:12px;padding:12px}
section{background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:10px 12px}
section h2{font-size:12px;text-transform:uppercase;letter-spacing:1px;color:var(--dim);
           margin:0 0 8px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:3px 6px;border-bottom:1px solid #eef1f5;font-size:12px}
th{color:var(--dim);font-weight:normal}
td.val{text-align:right;font-variant-numeric:tabular-nums}
tr.bad td{color:#c0392b}
.trendgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:10px}
.oee{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px}
.oee div{background:#f7f9fc;border:1px solid var(--line);border-radius:3px;padding:6px 8px}
.oee b{display:block;font-size:19px}
.oee span{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:1px}
button{font:inherit;border:1px solid var(--line);background:#fff;border-radius:3px;
       padding:1px 8px;cursor:pointer}
button:hover{background:#eef4ff}
.sev5{color:#c0392b}.sev4{color:#e07b1a}.sev3{color:#c9a227}.sev2{color:#4b7bec}
.foot{padding:6px 16px;color:var(--dim);font-size:11px}
.notice{padding:8px 16px;color:#7a5c00;background:#fff6d8;border-top:1px solid #e8d493;
        font-size:11px}
@media(max-width:900px){main{grid-template-columns:1fr}}
</style></head><body>
<header><h1>factorylink</h1>
  <div class="meta"><span id="clock">--</span> &middot; scans <span id="scans">0</span>
  &middot; <span id="link">connecting</span></div></header>
<div id="banner"></div>
<main>
  <section style="grid-column:1/-1"><h2>Trends</h2>
    <div class="trendgrid" id="trends"></div></section>
  <section><h2>Live values</h2>
    <table><thead><tr><th>tag</th><th>address</th><th>group</th>
      <th style="text-align:right">value</th><th>quality</th></tr></thead>
      <tbody id="tags"></tbody></table></section>
  <section><h2>OEE</h2><div class="oee" id="oee"></div>
    <h2>Downtime Pareto</h2>
    <table><thead><tr><th>reason</th><th style="text-align:right">min</th>
      <th style="text-align:right">share</th></tr></thead>
      <tbody id="pareto"></tbody></table></section>
  <section style="grid-column:1/-1"><h2>Alarms
    <button onclick="ack('*')">acknowledge all</button></h2>
    <table><thead><tr><th>alarm</th><th>tag</th><th>state</th><th>severity</th>
      <th style="text-align:right">value</th><th></th></tr></thead>
      <tbody id="alarms"></tbody></table></section>
</main>
<div class="notice" id="notice"></div>
<div class="foot" id="foot"></div>
<script>
var SEV = {5:"#c0392b",4:"#e07b1a",3:"#c9a227",2:"#4b7bec",1:"#7f8c8d"};
function fmt(x){ return x === null || x === undefined ? "--" : x; }
function render(s){
  document.getElementById("clock").textContent = new Date(s.now*1000).toISOString().slice(11,19);
  document.getElementById("scans").textContent = s.poller ? s.poller.scans : 0;
  var tb = document.getElementById("tags"); tb.innerHTML = "";
  s.tags.forEach(function(t){
    var tr = document.createElement("tr");
    if(t.quality !== "good"){ tr.className = "bad"; }
    tr.innerHTML = "<td title='"+fmt(t.description)+"'>"+t.name+"</td><td>"+t.address+
      "</td><td>"+t.group+"</td><td class='val'>"+t.display+"</td><td>"+t.quality+"</td>";
    tb.appendChild(tr);
  });
  var ab = document.getElementById("alarms"); ab.innerHTML = "";
  s.alarms.forEach(function(a){
    var tr = document.createElement("tr");
    tr.innerHTML = "<td class='sev"+a.severity+"'>"+a.name+"</td><td>"+a.tag+"</td><td>"+
      a.state+"</td><td class='sev"+a.severity+"'>"+a.severity_name+"</td><td class='val'>"+
      fmt(a.value)+"</td><td>"+(a.unacked ?
      "<button onclick=\\"ack('"+a.name+"')\\">ack</button>" : "") + "</td>";
    ab.appendChild(tr);
  });
  var banner = document.getElementById("banner");
  if(s.alarms.length){
    var worst = s.alarms[0];
    banner.style.display = "block";
    banner.style.background = SEV[worst.severity] || "#7f8c8d";
    banner.textContent = worst.severity_name + ": " + worst.message +
      (s.alarm_summary && s.alarm_summary.flood ? "   [ALARM FLOOD]" : "") +
      "   (" + s.alarms.length + " active)";
  } else { banner.style.display = "none"; }
  var o = document.getElementById("oee"); o.innerHTML = "";
  if(s.oee){
    [["Availability",s.oee.availability],["Performance",s.oee.performance],
     ["Quality",s.oee.quality],["OEE",s.oee.oee]].forEach(function(p){
      var d = document.createElement("div");
      d.innerHTML = "<span>"+p[0]+"</span><b>"+(p[1]*100).toFixed(1)+"%</b>";
      o.appendChild(d);
    });
    var pb = document.getElementById("pareto"); pb.innerHTML = "";
    s.oee.pareto.forEach(function(p){
      var tr = document.createElement("tr");
      tr.innerHTML = "<td>"+p.reason+"</td><td class='val'>"+p.minutes.toFixed(1)+
        "</td><td class='val'>"+(p.share*100).toFixed(0)+"%</td>";
      pb.appendChild(tr);
    });
  }
  var tg = document.getElementById("trends");
  tg.innerHTML = s.trends.map(function(t){ return t.svg; }).join("");
  if(s.safety){ document.getElementById("notice").textContent = s.safety.notice +
    "  Write mode: " + (s.safety.read_only ? "READ-ONLY" : "writes enabled for " +
    s.safety.allow_list.join(", ")); }
  if(s.poller){ document.getElementById("foot").textContent =
    s.poller.blocks.length + " coalesced read block(s): " + s.poller.blocks.join(" | "); }
}
function ack(name){
  fetch("/api/ack", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({alarm:name, operator:"web"})}).then(refresh);
}
function refresh(){
  fetch("/api/state").then(function(r){ return r.json(); })
    .then(render).catch(function(){});
}
if(window.EventSource){
  var es = new EventSource("/api/stream");
  es.onmessage = function(e){ document.getElementById("link").textContent = "sse";
    render(JSON.parse(e.data)); };
  es.onerror = function(){ document.getElementById("link").textContent = "polling";
    es.close(); setInterval(refresh, 1000); refresh(); };
} else { document.getElementById("link").textContent = "polling";
  setInterval(refresh, 1000); }
refresh();
</script></body></html>
"""


def make_handler(app: DashboardApp, stream_interval: float = 1.0) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class bound to ``app``."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "factorylink"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
            """Silence the default stderr access log."""

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            """Serve the page, the JSON snapshot, a single SVG, or the SSE stream."""
            parsed = urlparse(self.path)
            route = parsed.path
            query = parse_qs(parsed.query)
            if route in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif route == "/api/state":
                payload = json.dumps(app.snapshot(), default=str).encode("utf-8")
                self._send(200, payload, "application/json")
            elif route == "/api/health":
                self._send(200, b'{"ok":true}', "application/json")
            elif route == "/svg/trend":
                tag = (query.get("tag") or [""])[0]
                span = float((query.get("span") or ["300"])[0])
                svg = app.trend_svg(tag, span)
                self._send(200, svg.encode("utf-8"), "image/svg+xml")
            elif route == "/api/stream":
                self._stream()
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            """Handle alarm acknowledgement."""
            if urlparse(self.path).path != "/api/ack":
                self._send(404, b"not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                self._send(400, b'{"ok":false,"error":"bad json"}', "application/json")
                return
            result = app.acknowledge(str(body.get("alarm", "*")), str(body.get("operator", "web")))
            self._send(200, json.dumps(result).encode("utf-8"), "application/json")

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while not getattr(self.server, "shutting_down", False):
                    payload = json.dumps(app.snapshot(), default=str)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    threading.Event().wait(stream_interval)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    return Handler


def serve(
    app: DashboardApp,
    host: str = "127.0.0.1",
    port: int = 8377,
    stream_interval: float = 1.0,
) -> ThreadingHTTPServer:
    """Create (but do not start) an HTTP server for ``app``.

    The default bind address is loopback on purpose. A dashboard that exposes
    a write endpoint should not appear on a plant network by accident; binding
    0.0.0.0 must be a deliberate choice.
    """
    server = ThreadingHTTPServer((host, port), make_handler(app, stream_interval))
    server.daemon_threads = True
    server.shutting_down = False  # type: ignore[attr-defined]
    return server
