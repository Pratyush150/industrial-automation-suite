"""Poll scheduling, staggering, slow-scan detection and connection health."""

from __future__ import annotations

import pytest

from factorylink.poller import ConnectionHealth, PollGroup, Poller
from factorylink.protocols.base import Quality
from factorylink.protocols.simulator import SimulatedPLC, SimulatorDriver
from factorylink.tags import TagDatabase


@pytest.fixture()
def periods():
    """The shipped poll rates."""
    return {"fast": 0.5, "normal": 2.0, "slow": 10.0}


def test_groups_are_polled_at_their_own_rates(driver, db, clock, periods):
    """Over 20 s: fast ~40 scans, normal ~10, slow ~2."""
    poller = Poller({"line1": driver}, db, periods, clock=clock, stagger=False)
    poller.run(20.0)
    assert poller.groups["fast"].scans == 40
    assert poller.groups["normal"].scans == 10
    assert poller.groups["slow"].scans == 2


def test_slow_group_is_not_read_on_every_fast_scan(driver, db, clock, periods):
    """The whole point of poll groups: the slow tags cost almost nothing."""
    poller = Poller({"line1": driver}, db, periods, clock=clock, stagger=False)
    poller.run(60.0)
    fast_reads = poller.groups["fast"].scans * len(poller.groups["fast"].tags)
    slow_reads = poller.groups["slow"].scans * len(poller.groups["slow"].tags)
    assert slow_reads * 20 < fast_reads


def test_staggering_spreads_groups_that_share_a_period(db, clock, driver):
    """Two groups on the same period must not both fire at t=0."""
    tags = [
        {"name": "a", "address": 0, "poll_group": "g1"},
        {"name": "b", "address": 1, "poll_group": "g2"},
    ]
    small = TagDatabase.from_dicts(tags)
    plc = SimulatedPLC()
    drv = SimulatorDriver(plc, small, clock=clock)
    drv.connect()
    poller = Poller({"plc1": drv}, small, {"g1": 4.0, "g2": 4.0}, clock=clock, stagger=True)
    phases = sorted(g.phase for g in poller.groups.values())
    assert phases == [0.0, 2.0]
    assert len(poller.due_groups(0.0)) == 1


def test_staggering_can_be_disabled(driver, db, clock, periods):
    """Without staggering every group is due immediately."""
    poller = Poller({"line1": driver}, db, periods, clock=clock, stagger=False)
    assert all(g.phase == 0.0 for g in poller.groups.values())
    assert len(poller.due_groups(0.0)) == 3


def test_schedule_does_not_drift_after_a_slow_scan(db, clock):
    """The next scan is placed on the original grid, not 'now + period'."""
    plc = SimulatedPLC()
    drv = SimulatorDriver(plc, db, clock=clock, latency_s=0.05)
    drv.connect()
    poller = Poller({"line1": drv}, db, {"fast": 1.0}, clock=clock, stagger=False)
    poller.poll_group(poller.groups["fast"])
    assert poller.groups["fast"].next_due == pytest.approx(1.0)
    poller.poll_group(poller.groups["fast"])
    assert poller.groups["fast"].next_due == pytest.approx(2.0)


def test_slow_scan_is_flagged_as_an_overrun(db, clock):
    """A scan that eats 80% of its period leaves no headroom for a retry."""
    plc = SimulatedPLC()
    drv = SimulatorDriver(plc, db, clock=clock, latency_s=0.6)
    drv.connect()
    poller = Poller({"line1": drv}, db, {"fast": 0.5}, clock=clock, stagger=False)
    result = poller.poll_group(poller.groups["fast"])
    assert result.duration == pytest.approx(0.6)
    assert result.overrun is True
    assert poller.groups["fast"].overruns == 1
    assert poller.groups["fast"].duty > 1.0


def test_a_fast_scan_is_not_flagged(db, clock):
    """No false positives on a healthy loop."""
    plc = SimulatedPLC()
    drv = SimulatorDriver(plc, db, clock=clock, latency_s=0.01)
    drv.connect()
    poller = Poller({"line1": drv}, db, {"fast": 0.5}, clock=clock, stagger=False)
    result = poller.poll_group(poller.groups["fast"])
    assert result.overrun is False
    assert poller.groups["fast"].overruns == 0


def test_overrunning_scan_skips_periods_instead_of_queueing_up(db, clock):
    """A scan slower than its period must not build an unbounded backlog."""
    plc = SimulatedPLC()
    drv = SimulatorDriver(plc, db, clock=clock, latency_s=1.3)
    drv.connect()
    poller = Poller({"line1": drv}, db, {"fast": 0.5}, clock=clock, stagger=False)
    poller.poll_group(poller.groups["fast"])
    assert poller.groups["fast"].next_due > clock.now()


def test_connection_backoff_doubles_and_is_capped():
    """Retrying a dead device every scan takes the healthy devices down too."""
    health = ConnectionHealth("plc1", base_backoff=0.5, max_backoff=8.0)
    delays = []
    for i in range(8):
        health.record_failure(float(i), "boom")
        delays.append(health.backoff())
    assert delays[:5] == [0.5, 1.0, 2.0, 4.0, 8.0]
    assert max(delays) == 8.0
    assert health.consecutive_failures == 8
    assert health.connected is False


def test_recovery_resets_the_backoff_and_counts_a_reconnect():
    """A successful read clears the failure state."""
    health = ConnectionHealth("plc1")
    health.record_failure(0.0, "boom")
    health.record_failure(1.0, "boom")
    assert health.backoff() > 0
    health.record_success(2.0)
    assert health.consecutive_failures == 0
    assert health.connected is True
    assert health.reconnects == 1
    assert health.backoff() == 0.0


def test_backoff_suppresses_retries_until_it_expires():
    """may_retry is the gate the poller uses before calling connect()."""
    health = ConnectionHealth("plc1", base_backoff=2.0)
    health.record_failure(10.0, "boom")
    assert health.may_retry(11.0) is False
    assert health.may_retry(12.0) is True


def test_a_dead_device_yields_bad_quality_not_an_exception(flaky_driver, clock):
    """One dead PLC must not stop the scan or look like a value of zero."""
    small = TagDatabase.from_dicts([{"name": "a", "address": 0, "poll_group": "fast"}])
    poller = Poller({"plc1": flaky_driver}, small, {"fast": 1.0}, clock=clock, stagger=False)
    flaky_driver.device = "plc1"
    result = poller.poll_group(poller.groups["fast"])
    assert result.readings["a"].quality is Quality.BAD
    assert result.readings["a"].value is None
    assert result.errors
    assert poller.health["plc1"].connected is False


def test_the_poller_stops_hammering_a_dead_device(flaky_driver, clock):
    """Connect attempts must be spaced by the backoff, not one per scan."""
    small = TagDatabase.from_dicts([{"name": "a", "address": 0, "poll_group": "fast"}])
    poller = Poller({"plc1": flaky_driver}, small, {"fast": 0.1}, clock=clock, stagger=False)
    poller.run(30.0)
    assert poller.groups["fast"].scans == 300
    assert flaky_driver.connect_attempts < 20


def test_the_poller_reconnects_when_the_device_comes_back(flaky_driver, clock):
    """Once connect() succeeds the readings become GOOD again."""
    small = TagDatabase.from_dicts([{"name": "a", "address": 0, "poll_group": "fast"}])
    poller = Poller({"plc1": flaky_driver}, small, {"fast": 1.0}, clock=clock, stagger=False)
    poller.run(5.0)
    assert poller.values["a"].quality is Quality.BAD
    flaky_driver.fail_connect = False
    poller.run(60.0)
    assert poller.values["a"].quality is Quality.GOOD
    assert poller.health["plc1"].connected is True


def test_a_missing_driver_is_reported_rather_than_crashing(clock):
    """A tag pointing at a device with no driver is a config error."""
    small = TagDatabase.from_dicts(
        [{"name": "a", "address": 0, "device": "ghost", "poll_group": "fast"}]
    )
    poller = Poller({}, small, {"fast": 1.0}, clock=clock, stagger=False)
    result = poller.poll_group(poller.groups["fast"])
    assert "no driver configured" in result.errors[0]
    assert result.readings["a"].quality is Quality.BAD


def test_poll_group_rejects_a_non_positive_period():
    """A period of zero would spin the loop."""
    with pytest.raises(ValueError, match="positive period"):
        PollGroup("g", 0.0)


def test_read_efficiency_reports_requests_per_tag(driver, db, clock, periods):
    """A measured ratio, not a claim: fewer requests than tags in every group."""
    poller = Poller({"line1": driver}, db, periods, clock=clock)
    efficiency = poller.read_efficiency()
    assert set(efficiency) == {"fast", "normal", "slow"}
    assert all(value < 1.0 for value in efficiency.values())
    assert efficiency["fast"] == pytest.approx(2 / 21)


def test_snapshot_reports_groups_devices_and_blocks(driver, db, clock, periods):
    """The dashboard and CLI both read this structure."""
    poller = Poller({"line1": driver}, db, periods, clock=clock)
    poller.run(5.0)
    snapshot = poller.snapshot()
    assert snapshot["scans"] > 0
    assert len(snapshot["groups"]) == 3
    assert snapshot["devices"][0]["device"] == "line1"
    assert snapshot["devices"][0]["connected"] is True
    assert len(snapshot["blocks"]) == 7
