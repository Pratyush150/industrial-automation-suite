"""Historian: swinging-door compression, deadband, aggregation and retention."""

from __future__ import annotations

import math
import random

import pytest

from factorylink.historian import Historian, SwingingDoor
from factorylink.protocols.base import Quality, Reading


def reconstruct(points, timestamp: float) -> float:
    """Linear interpolation between archived points, as a trend viewer does."""
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t0 <= timestamp <= t1:
            if t1 == t0:
                return v0
            return v0 + (v1 - v0) * (timestamp - t0) / (t1 - t0)
    return points[-1][1]


def noisy_sine(count: int = 1000, sigma: float = 0.1, seed: int = 1):
    """A deterministic noisy signal used by several compression tests."""
    rng = random.Random(seed)
    return [(i * 0.5, 10.0 * math.sin(i * 0.5 / 20.0) + rng.gauss(0.0, sigma))
            for i in range(count)]


# -- swinging door ---------------------------------------------------------


def test_a_straight_ramp_compresses_to_two_points():
    """The headline property: a linear signal needs two points, not 200."""
    door = SwingingDoor(tolerance=0.5)
    archived = []
    for i in range(200):
        archived += door.update(i * 1.0, 0.05 * i)
    archived += door.flush()
    assert len(archived) == 2
    assert door.seen == 200
    assert door.compression_ratio < 0.02


def test_a_flat_signal_compresses_to_two_points():
    """A parked signal costs almost nothing to store."""
    door = SwingingDoor(tolerance=0.1)
    archived = []
    for i in range(500):
        archived += door.update(i * 1.0, 42.0)
    archived += door.flush()
    assert len(archived) == 2


def test_compression_preserves_the_signal_within_tolerance():
    """The reconstruction error stays inside the documented 2x bound.

    Swinging door guarantees +/-tolerance against the *door* slope. Rebuilding
    by joining archived points with straight lines -- which is what every trend
    viewer does -- costs up to 2x tolerance in the worst case. That bound is
    the honest one, so it is what the test asserts.
    """
    for tolerance in (0.25, 0.5, 1.0):
        original = noisy_sine()
        door = SwingingDoor(tolerance=tolerance)
        archived = []
        for timestamp, value in original:
            archived += door.update(timestamp, value)
        archived += door.flush()
        error = max(abs(v - reconstruct(archived, t)) for t, v in original)
        assert error <= 2.0 * tolerance
        assert len(archived) < len(original) * 0.1


def test_tighter_tolerance_keeps_more_points():
    """Tolerance is the dial between fidelity and storage."""
    counts = []
    for tolerance in (0.1, 0.5, 2.0):
        door = SwingingDoor(tolerance=tolerance)
        archived = []
        for timestamp, value in noisy_sine():
            archived += door.update(timestamp, value)
        archived += door.flush()
        counts.append(len(archived))
    assert counts[0] > counts[1] > counts[2]


def test_a_step_change_is_always_captured():
    """Compression must never hide a step; that is the whole risk."""
    door = SwingingDoor(tolerance=0.5)
    archived = []
    for i in range(50):
        archived += door.update(float(i), 10.0)
    for i in range(50, 100):
        archived += door.update(float(i), 40.0)
    archived += door.flush()
    values = [v for _, v in archived]
    assert min(values) == pytest.approx(10.0)
    assert max(values) == pytest.approx(40.0)
    assert reconstruct(archived, 80.0) == pytest.approx(40.0, abs=0.5)


def test_max_interval_forces_a_heartbeat_point():
    """A flat trend still has to prove the tag was being read."""
    door = SwingingDoor(tolerance=1.0, max_interval=10.0)
    archived = []
    for i in range(100):
        archived += door.update(float(i), 5.0)
    assert len(archived) >= 9


def test_out_of_order_samples_are_ignored():
    """A historian that trusts unsorted timestamps draws trends backwards."""
    door = SwingingDoor(tolerance=0.5)
    door.update(10.0, 1.0)
    assert door.update(5.0, 99.0) == []
    assert door.seen == 1


def test_negative_tolerance_is_rejected():
    """Config validation."""
    with pytest.raises(ValueError, match="tolerance"):
        SwingingDoor(tolerance=-1.0)


# -- deadband --------------------------------------------------------------


def test_deadband_suppresses_a_noisy_signal(clock):
    """Noise inside the deadband must not reach the database."""
    hist = Historian(clock=clock, default_deadband=0.5, default_max_interval=None)
    rng = random.Random(11)
    for i in range(400):
        hist.record("t", 20.0 + rng.gauss(0.0, 0.1), timestamp=float(i))
    assert hist.count("t") == 1


def test_deadband_passes_a_real_step(clock):
    """The same deadband must not hide a genuine change."""
    hist = Historian(clock=clock, default_deadband=0.5, default_max_interval=None)
    rng = random.Random(11)
    for i in range(200):
        hist.record("t", 20.0 + rng.gauss(0.0, 0.1), timestamp=float(i))
    before = hist.count("t")
    for i in range(200, 400):
        hist.record("t", 35.0 + rng.gauss(0.0, 0.1), timestamp=float(i))
    assert hist.count("t") > before
    samples = hist.query("t")
    assert max(s.value for s in samples) > 34.0
    assert min(s.value for s in samples) < 21.0


def test_compression_can_be_switched_off(clock):
    """Sometimes you want every sample; make that possible and explicit."""
    hist = Historian(clock=clock, default_deadband=5.0, compression=False)
    for i in range(50):
        hist.record("t", 20.0, timestamp=float(i))
    assert hist.count("t") == 50


def test_bad_quality_readings_are_not_archived(clock):
    """Storing a BAD reading as a number destroys the trend."""
    hist = Historian(clock=clock)
    hist.record_reading(Reading("t", None, 0.0, Quality.BAD, error="offline"))
    hist.record_reading(Reading("t", 5.0, 1.0, Quality.GOOD))
    assert hist.count("t") == 1


def test_booleans_are_stored_as_numbers(clock):
    """Digital tags belong in the same table, as 0 and 1."""
    hist = Historian(clock=clock, default_max_interval=None)
    hist.record_reading(Reading("bit", True, 0.0, Quality.GOOD))
    hist.record_reading(Reading("bit", False, 1.0, Quality.GOOD))
    assert [s.value for s in hist.query("bit")] == [1.0, 0.0]


# -- queries ---------------------------------------------------------------


def test_aggregation_produces_min_max_avg_per_interval(clock):
    """The query a trend page actually needs."""
    hist = Historian(clock=clock, compression=False)
    for i in range(120):
        hist.record("t", float(i), timestamp=float(i))
    buckets = hist.aggregate("t", 0.0, 120.0, 30.0)
    assert len(buckets) == 4
    assert buckets[0].count == 30
    assert buckets[0].minimum == 0.0
    assert buckets[0].maximum == 29.0
    assert buckets[0].average == pytest.approx(14.5)
    assert buckets[0].first == 0.0
    assert buckets[0].last == 29.0
    assert buckets[-1].minimum == 90.0


def test_aggregation_rejects_a_non_positive_interval(clock):
    """Zero-width buckets are a caller bug."""
    hist = Historian(clock=clock)
    with pytest.raises(ValueError, match="interval"):
        hist.aggregate("t", 0.0, 10.0, 0.0)


def test_query_window_and_latest(clock):
    """Time-window selection and the most recent value."""
    hist = Historian(clock=clock, compression=False)
    for i in range(100):
        hist.record("t", float(i), timestamp=float(i))
    assert len(hist.query("t", 10.0, 19.0)) == 10
    assert hist.query("t", limit=5)[-1].timestamp == 4.0
    assert hist.latest("t").value == 99.0
    assert hist.latest("missing") is None


def test_interpolation_rebuilds_a_compressed_ramp(clock):
    """Reading back a compressed trend is linear interpolation."""
    hist = Historian(clock=clock, default_tolerance=0.5, default_max_interval=None)
    for i in range(100):
        hist.record("t", 0.1 * i, timestamp=float(i))
    hist.flush()
    assert hist.count("t") <= 4
    assert hist.interpolate("t", 50.0) == pytest.approx(5.0, abs=0.5)
    assert hist.interpolate("missing", 1.0) is None


def test_retention_deletes_old_samples(clock):
    """An unbounded SQLite file eventually fills the disk on the HMI box."""
    hist = Historian(clock=clock, compression=False)
    for i in range(1000):
        hist.record("t", float(i), timestamp=float(i))
    removed = hist.apply_retention(max_age=200.0, now=1000.0)
    assert removed == 800
    assert hist.count("t") == 200
    assert hist.query("t")[0].timestamp == 800.0


def test_events_table_records_alarm_transitions(clock):
    """An incident review needs the events alongside the trend."""
    hist = Historian(clock=clock)
    hist.record_event("t.hi", "raised", "high limit", 4, timestamp=10.0)
    hist.record_event("t.hi", "cleared", "back to normal", 4, timestamp=20.0)
    events = hist.events()
    assert len(events) == 2
    assert events[0]["kind"] == "cleared"
    assert events[0]["severity"] == 4


def test_csv_export_writes_every_archived_sample(tmp_path, clock):
    """Export is how the data leaves for a spreadsheet or a report."""
    hist = Historian(clock=clock, compression=False)
    for i in range(25):
        hist.record("t", float(i), timestamp=float(i))
        hist.record("u", float(i) * 2, timestamp=float(i))
    path = tmp_path / "history.csv"
    rows = hist.export_csv(path)
    assert rows == 50
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert lines[0] == "tag,timestamp,value,quality"
    assert len(lines) == 51
    only_t = hist.export_csv(tmp_path / "t.csv", ["t"])
    assert only_t == 25


def test_stats_report_the_achieved_compression(clock):
    """A measured ratio, not a claim."""
    hist = Historian(clock=clock, default_deadband=0.5, default_tolerance=1.0,
                     default_max_interval=None)
    for i in range(500):
        hist.record("t", 0.01 * i, timestamp=float(i))
    hist.flush()
    stats = hist.stats()
    assert stats["received"] == 500
    assert stats["stored"] < 20
    assert 0.0 < stats["ratio"] < 0.1
    assert stats["tags"]["t"]["deadband"] == 0.5


def test_configure_from_tags_derives_tolerances(db, clock):
    """Compression deviation defaults to twice the exception deadband."""
    hist = Historian(clock=clock)
    hist.configure_from_tags(db, tolerance_factor=2.0)
    assert hist.policies["tank_level"].deadband == pytest.approx(0.2)
    assert hist.policies["tank_level"].tolerance == pytest.approx(0.4)
    assert hist.policies["tank_level"].door is not None


def test_historian_can_be_used_as_a_context_manager(tmp_path, clock):
    """Closing must flush the pending swinging-door point."""
    path = tmp_path / "h.sqlite"
    with Historian(path, clock=clock, default_tolerance=0.5, default_max_interval=None) as hist:
        for i in range(50):
            hist.record("t", 0.1 * i, timestamp=float(i))
    with Historian(path, clock=clock) as reopened:
        assert reopened.count("t") >= 2
        assert reopened.tags() == ["t"]
