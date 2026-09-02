"""OEE arithmetic against a hand-computed example, plus downtime Pareto."""

from __future__ import annotations

import pytest

from factorylink.oee import (
    DowntimeEvent,
    DowntimeTracker,
    OEECalculator,
    compute_oee,
)


def test_matches_a_hand_computed_example():
    """Worked by hand, then pinned.

    Planned production time 30,000 s with 6,000 s of unplanned downtime, so
    run time is 24,000 s and availability is 24000/30000 = 0.80.
    9,600 units at an ideal cycle time of 2.0 s is 19,200 s of ideal run time,
    so performance is 19200/24000 = 0.80.
    960 of the 9,600 were rejected, so quality is 8640/9600 = 0.90.
    OEE = 0.80 x 0.80 x 0.90 = 0.576.
    """
    result = compute_oee(
        planned_time=30000.0,
        downtime=6000.0,
        ideal_cycle_time=2.0,
        total_count=9600,
        reject_count=960,
    )
    assert result.run_time == 24000.0
    assert result.good_count == 8640
    assert result.availability == pytest.approx(0.80)
    assert result.performance == pytest.approx(0.80)
    assert result.quality == pytest.approx(0.90)
    assert result.oee == pytest.approx(0.576)
    assert result.performance_clamped is False


def test_performance_uses_total_count_not_good_count():
    """Using good count in both places double-counts the quality loss."""
    with_rejects = compute_oee(1000.0, 0.0, 1.0, 900, 100)
    without = compute_oee(1000.0, 0.0, 1.0, 900, 0)
    assert with_rejects.performance == pytest.approx(without.performance)
    assert with_rejects.quality < without.quality


def test_planned_downtime_is_excluded_from_availability():
    """A scheduled changeover is not an availability loss."""
    result = compute_oee(
        planned_time=27000.0,
        downtime=0.0,
        ideal_cycle_time=1.0,
        total_count=27000,
        planned_downtime=1800.0,
    )
    assert result.availability == pytest.approx(1.0)
    assert result.planned_downtime == 1800.0
    assert result.teep_hint < result.oee


def test_performance_above_one_is_flagged_not_hidden():
    """Beating the ideal cycle time means the ideal cycle time is wrong."""
    result = compute_oee(1000.0, 0.0, 2.0, 900, 0)
    assert result.performance_raw == pytest.approx(1.8)
    assert result.performance == 1.0
    assert result.performance_clamped is True
    assert any("ideal cycle time" in note for note in result.notes)


def test_clamping_can_be_disabled():
    """Some reports want the raw number."""
    result = compute_oee(1000.0, 0.0, 2.0, 900, 0, clamp_performance=False)
    assert result.performance == pytest.approx(1.8)
    assert result.performance_clamped is False


def test_zero_planned_time_is_reported_not_divided_by():
    """A shift that never started must not raise a ZeroDivisionError."""
    result = compute_oee(0.0, 0.0, 1.0, 0, 0)
    assert result.availability == 0.0
    assert result.oee == 0.0
    assert result.notes


def test_loss_breakdown_sums_with_oee():
    """The three losses plus OEE account for the whole theoretical output."""
    result = compute_oee(30000.0, 6000.0, 2.0, 9600, 960)
    losses = result.losses
    assert sum(losses.values()) + result.oee == pytest.approx(1.0)
    assert losses["availability"] == pytest.approx(0.20)


def test_invalid_inputs_are_rejected():
    """Bad inputs are a caller bug, not something to average over."""
    with pytest.raises(ValueError, match="ideal cycle time"):
        compute_oee(100.0, 0.0, 0.0, 10, 0)
    with pytest.raises(ValueError, match="negative"):
        compute_oee(-1.0, 0.0, 1.0, 10, 0)
    with pytest.raises(ValueError, match="exceeds total count"):
        compute_oee(100.0, 0.0, 1.0, 10, 20)
    with pytest.raises(ValueError, match="exceeds planned time"):
        compute_oee(100.0, 200.0, 1.0, 10, 0)


def test_report_text_contains_the_four_numbers():
    """The report is a deliverable; its shape is pinned."""
    text = compute_oee(30000.0, 6000.0, 2.0, 9600, 960).format_report()
    assert "Availability  80.00%" in text
    assert "Performance   80.00%" in text
    assert "Quality       90.00%" in text
    assert "OEE           57.60%" in text


# -- downtime tracking -----------------------------------------------------


def test_pareto_ranks_causes_and_accumulates_share():
    """OEE says there is a loss; the Pareto says what to go and fix."""
    tracker = DowntimeTracker(
        [
            DowntimeEvent(0, 600, "Conveyor jam"),
            DowntimeEvent(700, 1000, "Conveyor jam"),
            DowntimeEvent(1100, 2300, "Changeover", "planned"),
            DowntimeEvent(2400, 2700, "Capper fault"),
            DowntimeEvent(2800, 2900, "Label misfeed"),
        ]
    )
    rows = tracker.pareto()
    assert [r.reason for r in rows] == ["Conveyor jam", "Capper fault", "Label misfeed"]
    assert rows[0].events == 2
    assert rows[0].seconds == 900.0
    assert rows[0].minutes == pytest.approx(15.0)
    assert rows[0].share == pytest.approx(900 / 1300)
    assert rows[-1].cumulative == pytest.approx(1.0)


def test_planned_and_unplanned_totals_are_separated():
    """Mixing them is the most common way to make OEE meaningless."""
    tracker = DowntimeTracker(
        [DowntimeEvent(0, 100, "Jam"), DowntimeEvent(200, 500, "Changeover", "planned")]
    )
    assert tracker.unplanned() == 100.0
    assert tracker.planned() == 300.0
    assert tracker.total() == 400.0


def test_open_stop_contributes_nothing_until_closed():
    """A stop in progress has no duration yet."""
    tracker = DowntimeTracker()
    tracker.start_stop(100.0, "Jam")
    assert tracker.unplanned() == 0.0
    assert tracker.open_event is not None
    tracker.end_stop(160.0)
    assert tracker.unplanned() == 60.0
    assert tracker.open_event is None


def test_a_second_start_while_stopped_is_ignored():
    """Two stop signals in a row are one stop."""
    tracker = DowntimeTracker()
    first = tracker.start_stop(10.0, "Jam")
    second = tracker.start_stop(12.0, "Jam again")
    assert first is second
    assert len(tracker.events) == 1


def test_pareto_text_is_renderable():
    """Used verbatim by the CLI."""
    tracker = DowntimeTracker([DowntimeEvent(0, 600, "Conveyor jam")])
    text = tracker.format_pareto()
    assert "DOWNTIME PARETO" in text
    assert "Conveyor jam" in text
    assert "100.0%" in text
    assert "no downtime recorded" in DowntimeTracker().format_pareto()


# -- calculator ------------------------------------------------------------


def test_calculator_builds_the_result_from_state_transitions():
    """Feed run/stop transitions and counters; get an OEE result."""
    calc = OEECalculator(ideal_cycle_time=0.5, shift_start=0.0)
    calc.record_state(0.0, running=True)
    calc.record_state(600.0, running=False, reason="Conveyor jam")
    calc.record_state(900.0, running=True)
    calc.update_counts(total_count=6000, reject_count=60)
    result = calc.result(now=3600.0)
    assert result.planned_time == 3600.0
    assert result.downtime == pytest.approx(300.0)
    assert result.run_time == pytest.approx(3300.0)
    assert result.quality == pytest.approx(0.99)
    assert calc.pareto()[0].reason == "Conveyor jam"


def test_calculator_closes_an_open_stop_at_end_of_shift():
    """A line that is still down when the shift ends still counts."""
    calc = OEECalculator(ideal_cycle_time=1.0, shift_start=0.0)
    calc.record_state(100.0, running=False, reason="Jam")
    calc.update_counts(100, 0)
    result = calc.result(now=200.0)
    assert result.downtime == pytest.approx(100.0)


def test_calculator_can_load_events_from_dicts():
    """Downtime history often arrives as rows from another system."""
    calc = OEECalculator(ideal_cycle_time=1.0)
    calc.load_events(
        [
            {"start": 0, "end": 60, "reason": "Jam"},
            {"start": 120, "end": 240, "reason": "Changeover", "category": "planned"},
        ]
    )
    calc.update_counts(500, 5)
    result = calc.result(now=1000.0)
    assert result.planned_downtime == 120.0
    assert result.planned_time == 880.0
    assert result.downtime == 60.0


def test_calculator_rejects_bad_inputs():
    """Guard the constructor and the counters."""
    with pytest.raises(ValueError, match="ideal cycle time"):
        OEECalculator(0.0)
    calc = OEECalculator(1.0)
    with pytest.raises(ValueError, match="negative"):
        calc.update_counts(-1, 0)
