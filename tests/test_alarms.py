"""Alarm engine: hysteresis, delays, latching, shelving and flood detection.

The chatter tests are the important ones. An alarm system without hysteresis
and delays is worse than no alarm system, because it trains operators to
ignore the list.
"""

from __future__ import annotations

import pytest

from factorylink.alarms import (
    AlarmEngine,
    AlarmSpec,
    AlarmState,
    AlarmType,
    Severity,
    spec_from_mapping,
    specs_from_tags,
)


def spec(**kwargs) -> AlarmSpec:
    """An alarm spec with sensible test defaults."""
    base = dict(name="a1", tag="t", alarm_type=AlarmType.HI, limit=10.0, severity=Severity.HIGH)
    base.update(kwargs)
    return AlarmSpec(**base)  # type: ignore[arg-type]


def drive(engine: AlarmEngine, samples, tag: str = "t"):
    """Feed (time, value) samples and return the flat list of events."""
    events = []
    for timestamp, value in samples:
        events.extend(engine.update({tag: value}, now=timestamp))
    return events


# -- hysteresis ------------------------------------------------------------


def test_no_deadband_chatters_at_the_threshold(clock):
    """The failure mode: a noisy signal on the limit toggles every scan.

    This is the behaviour the rest of the module exists to prevent, so it is
    pinned as a test rather than described in a comment.
    """
    engine = AlarmEngine([spec(deadband=0.0)], clock=clock)
    samples = [(i * 0.5, 10.0 + (0.05 if i % 2 == 0 else -0.05)) for i in range(40)]
    events = drive(engine, samples)
    raised = [e for e in events if e.kind == "raised"]
    assert len(raised) >= 15


def test_deadband_stops_the_chatter(clock):
    """The same signal with a deadband raises once and stays raised."""
    engine = AlarmEngine([spec(deadband=1.0)], clock=clock)
    samples = [(i * 0.5, 10.0 + (0.05 if i % 2 == 0 else -0.05)) for i in range(40)]
    events = drive(engine, samples)
    assert [e.kind for e in events] == ["raised"]
    assert engine.alarms["a1"].activations == 1


def test_deadband_still_clears_on_a_real_recovery(clock):
    """Hysteresis must not prevent the alarm ever clearing."""
    engine = AlarmEngine([spec(deadband=1.0)], clock=clock)
    drive(engine, [(0.0, 9.0), (1.0, 11.0)])
    assert engine.alarms["a1"].state is AlarmState.ACTIVE_UNACK
    drive(engine, [(2.0, 9.5)])
    assert engine.alarms["a1"].active is True  # inside the deadband, still alarming
    drive(engine, [(3.0, 8.5)])
    assert engine.alarms["a1"].state is AlarmState.NORMAL


def test_low_alarm_hysteresis_works_in_the_other_direction(clock):
    """A LO alarm raises going down and clears going up past limit+deadband."""
    engine = AlarmEngine(
        [spec(name="lo1", alarm_type=AlarmType.LO, limit=20.0, deadband=2.0)], clock=clock
    )
    drive(engine, [(0.0, 25.0), (1.0, 19.0)])
    assert engine.alarms["lo1"].active is True
    drive(engine, [(2.0, 21.0)])
    assert engine.alarms["lo1"].active is True
    drive(engine, [(3.0, 22.5)])
    assert engine.alarms["lo1"].active is False


# -- delays ----------------------------------------------------------------


def test_on_delay_ignores_a_transient(clock):
    """A single spike shorter than the on-delay must not produce an event."""
    engine = AlarmEngine([spec(on_delay=3.0)], clock=clock)
    events = drive(engine, [(0.0, 9.0), (1.0, 50.0), (2.0, 9.0), (3.0, 9.0), (10.0, 9.0)])
    assert events == []
    assert engine.alarms["a1"].state is AlarmState.NORMAL


def test_on_delay_fires_for_a_sustained_condition(clock):
    """The same limit, held past the delay, does alarm."""
    engine = AlarmEngine([spec(on_delay=3.0)], clock=clock)
    events = drive(engine, [(0.0, 9.0), (1.0, 50.0), (2.0, 50.0), (5.0, 50.0)])
    assert [e.kind for e in events] == ["raised"]
    assert events[0].timestamp == 5.0


def test_off_delay_holds_the_alarm_through_a_dip(clock):
    """A signal hovering at the limit should not flicker back to normal."""
    engine = AlarmEngine([spec(off_delay=4.0)], clock=clock)
    drive(engine, [(0.0, 20.0)])
    assert engine.alarms["a1"].active is True
    drive(engine, [(1.0, 5.0), (2.0, 20.0), (3.0, 5.0)])
    assert engine.alarms["a1"].active is True
    drive(engine, [(8.0, 5.0)])
    assert engine.alarms["a1"].active is False


# -- latching and acknowledgement -----------------------------------------


def test_latched_alarm_stays_until_acknowledged(clock):
    """After the process recovers, a latched alarm is still on the list."""
    engine = AlarmEngine([spec(latched=True)], clock=clock)
    drive(engine, [(0.0, 20.0), (1.0, 1.0)])
    instance = engine.alarms["a1"]
    assert instance.active is False
    assert instance.state is AlarmState.RTN_UNACK
    assert instance.unacknowledged is True
    assert len(engine.annunciated()) == 1
    engine.acknowledge("a1", by="tester", now=5.0)
    assert instance.state is AlarmState.NORMAL
    assert instance.acked_by == "tester"
    assert engine.annunciated() == []


def test_unlatched_alarm_clears_itself(clock):
    """A non-latching alarm returns to normal without an operator."""
    engine = AlarmEngine([spec(latched=False)], clock=clock)
    drive(engine, [(0.0, 20.0), (1.0, 1.0)])
    assert engine.alarms["a1"].state is AlarmState.NORMAL
    assert engine.annunciated() == []


def test_acknowledging_an_active_alarm_keeps_it_active(clock):
    """Acknowledgement silences, it does not fix the process."""
    engine = AlarmEngine([spec(latched=True)], clock=clock)
    drive(engine, [(0.0, 20.0)])
    engine.acknowledge("a1", now=1.0)
    instance = engine.alarms["a1"]
    assert instance.state is AlarmState.ACTIVE_ACK
    assert instance.active is True
    assert instance.unacknowledged is False


def test_acknowledge_all_clears_every_pending_alarm(clock):
    """The dashboard's 'acknowledge all' button."""
    engine = AlarmEngine(
        [spec(name="a1", latched=True), spec(name="a2", tag="u", latched=True)], clock=clock
    )
    engine.update({"t": 20.0, "u": 20.0}, now=0.0)
    assert len(engine.annunciated()) == 2
    engine.acknowledge_all(by="web", now=1.0)
    assert all(not i.unacknowledged for i in engine.alarms.values())


def test_acknowledging_an_unknown_alarm_raises(clock):
    """A typo in an ack request must not be silently ignored."""
    engine = AlarmEngine([spec()], clock=clock)
    with pytest.raises(KeyError, match="unknown alarm"):
        engine.acknowledge("nope")


# -- shelving --------------------------------------------------------------


def test_shelving_suppresses_an_alarm_for_a_bounded_time(clock):
    """A known-bad instrument can be silenced on the record, not ignored."""
    engine = AlarmEngine([spec()], clock=clock)
    clock.set(0.0)
    engine.shelve("a1", duration=100.0, by="tester")
    assert engine.alarms["a1"].shelved is True
    drive(engine, [(10.0, 50.0), (50.0, 50.0)])
    assert engine.alarms["a1"].active is False
    assert engine.annunciated() == []


def test_shelving_expires_and_the_alarm_comes_back(clock):
    """Bounded is the point: the alarm returns to service by itself."""
    engine = AlarmEngine([spec()], clock=clock)
    clock.set(0.0)
    engine.shelve("a1", duration=100.0)
    drive(engine, [(50.0, 50.0)])
    assert engine.alarms["a1"].active is False
    drive(engine, [(150.0, 50.0), (151.0, 50.0)])
    assert engine.alarms["a1"].active is True


def test_shelving_requires_a_positive_duration(clock):
    """An unbounded shelve is a disabled alarm nobody remembers disabling."""
    engine = AlarmEngine([spec()], clock=clock)
    with pytest.raises(ValueError, match="positive"):
        engine.shelve("a1", duration=0.0)


def test_unshelve_returns_an_alarm_to_service(clock):
    """Manual return to service before the shelf timer expires."""
    engine = AlarmEngine([spec()], clock=clock)
    clock.set(0.0)
    engine.shelve("a1", duration=1000.0)
    engine.unshelve("a1")
    assert engine.alarms["a1"].shelved is False
    drive(engine, [(1.0, 50.0)])
    assert engine.alarms["a1"].active is True


# -- other alarm types -----------------------------------------------------


def test_rate_of_change_alarm(clock):
    """Catch a temperature runaway before it reaches the high limit."""
    engine = AlarmEngine(
        [spec(name="roc", alarm_type=AlarmType.ROC_HI, limit=0.5, roc_window=10.0)], clock=clock
    )
    drive(engine, [(float(t), 4.0 + 0.1 * t) for t in range(20)])
    assert engine.alarms["roc"].active is False
    drive(engine, [(20.0 + t, 6.0 + 2.0 * t) for t in range(10)])
    assert engine.alarms["roc"].active is True


def test_rate_of_change_low_alarm(clock):
    """A tank draining fast is a different alarm from a tank that is empty."""
    engine = AlarmEngine(
        [spec(name="roc", alarm_type=AlarmType.ROC_LO, limit=0.5, roc_window=10.0)], clock=clock
    )
    drive(engine, [(float(t), 90.0 - 5.0 * t) for t in range(12)])
    assert engine.alarms["roc"].active is True


def test_deviation_alarm_follows_a_setpoint_tag(clock):
    """Deviation is measured against a live setpoint, not a constant."""
    engine = AlarmEngine(
        [
            AlarmSpec(
                name="dev",
                tag="pv",
                alarm_type=AlarmType.DEVIATION,
                limit=3.0,
                setpoint_tag="sp",
                severity=Severity.MEDIUM,
            )
        ],
        clock=clock,
    )
    engine.update({"pv": 30.0, "sp": 30.0}, now=0.0)
    assert engine.alarms["dev"].active is False
    engine.update({"pv": 25.0, "sp": 30.0}, now=1.0)
    assert engine.alarms["dev"].active is True
    engine.update({"pv": 25.0, "sp": 25.5}, now=2.0)
    assert engine.alarms["dev"].active is False


def test_digital_alarm_on_a_boolean(clock):
    """Some alarms are just a bit being set."""
    engine = AlarmEngine(
        [spec(name="jam", tag="sts_jam", alarm_type=AlarmType.DIGITAL, trigger_state=True)],
        clock=clock,
    )
    engine.update({"sts_jam": False}, now=0.0)
    assert engine.alarms["jam"].active is False
    engine.update({"sts_jam": True}, now=1.0)
    assert engine.alarms["jam"].active is True


def test_digital_alarm_can_trigger_on_a_cleared_bit(clock):
    """A healthy signal going false is the alarm."""
    engine = AlarmEngine(
        [spec(name="estop", tag="ok", alarm_type=AlarmType.DIGITAL, trigger_state=False)],
        clock=clock,
    )
    engine.update({"ok": True}, now=0.0)
    assert engine.alarms["estop"].active is False
    engine.update({"ok": False}, now=1.0)
    assert engine.alarms["estop"].active is True


# -- quality, flood, severity ---------------------------------------------


def test_bad_quality_holds_the_alarm_state(clock):
    """A comms failure must never look like the process recovering."""
    engine = AlarmEngine([spec()], clock=clock)
    drive(engine, [(0.0, 50.0)])
    assert engine.alarms["a1"].active is True
    engine.update({"t": None}, now=1.0)
    assert engine.alarms["a1"].active is True


def test_flood_detection_fires_above_the_threshold(clock):
    """Ten alarms in ten minutes is the EEMUA flood definition."""
    specs = [spec(name=f"a{i}", tag=f"t{i}") for i in range(15)]
    engine = AlarmEngine(specs, clock=clock, flood_count=10, flood_window=600.0)
    events = engine.update({f"t{i}": 50.0 for i in range(15)}, now=0.0)
    kinds = [e.kind for e in events]
    assert kinds.count("raised") == 15
    assert "flood" in kinds
    assert engine.in_flood is True
    assert engine.summary()["flood"] is True


def test_flood_clears_once_the_rate_falls(clock):
    """The flood indication must not latch forever."""
    specs = [spec(name=f"a{i}", tag=f"t{i}") for i in range(15)]
    engine = AlarmEngine(specs, clock=clock, flood_count=10, flood_window=600.0)
    engine.update({f"t{i}": 50.0 for i in range(15)}, now=0.0)
    events = engine.update({f"t{i}": 50.0 for i in range(15)}, now=1000.0)
    assert any(e.kind == "flood_cleared" for e in events)
    assert engine.in_flood is False


def test_a_single_undeadbanded_alarm_can_flood_by_itself(clock):
    """One chattering tag at a 0.5 s scan exceeds the flood threshold alone."""
    engine = AlarmEngine([spec(deadband=0.0)], clock=clock, flood_count=10, flood_window=600.0)
    drive(engine, [(i * 0.5, 10.0 + (0.05 if i % 2 == 0 else -0.05)) for i in range(60)])
    assert engine.rate(600.0, now=30.0) > 10
    assert engine.in_flood is True


def test_annunciated_list_is_sorted_worst_first(clock):
    """Operators read from the top; severity has to drive the order."""
    engine = AlarmEngine(
        [
            spec(name="low", tag="t1", severity=Severity.LOW),
            spec(name="crit", tag="t2", severity=Severity.CRITICAL),
            spec(name="med", tag="t3", severity=Severity.MEDIUM),
        ],
        clock=clock,
    )
    engine.update({"t1": 50.0, "t2": 50.0, "t3": 50.0}, now=0.0)
    assert [i.spec.name for i in engine.annunciated()] == ["crit", "med", "low"]
    assert engine.worst_severity() is Severity.CRITICAL


def test_summary_counts_by_severity(clock):
    """The dashboard banner reads this."""
    engine = AlarmEngine(
        [spec(name="c", tag="t1", severity=Severity.CRITICAL, latched=True),
         spec(name="h", tag="t2", severity=Severity.HIGH)],
        clock=clock,
    )
    engine.update({"t1": 50.0, "t2": 50.0}, now=0.0)
    summary = engine.summary()
    assert summary["configured"] == 2
    assert summary["active"] == 2
    assert summary["by_severity"] == {"CRITICAL": 1, "HIGH": 1}
    assert summary["worst"] == "CRITICAL"


def test_disabled_alarms_are_skipped(clock):
    """An alarm turned off in config must not fire."""
    engine = AlarmEngine([spec(enabled=False)], clock=clock)
    assert drive(engine, [(0.0, 100.0)]) == []


def test_duplicate_alarm_names_are_rejected():
    """Two alarms with the same name would overwrite each other's state."""
    with pytest.raises(ValueError, match="duplicate alarm name"):
        AlarmEngine([spec(name="x"), spec(name="x", tag="u")])


def test_negative_delays_and_deadbands_are_rejected():
    """Config validation, not a runtime surprise."""
    with pytest.raises(ValueError, match="deadband"):
        spec(deadband=-1.0)
    with pytest.raises(ValueError, match="delays"):
        spec(on_delay=-1.0)


def test_specs_generated_from_tags_get_deadbands_and_delays(db):
    """Generating chattering alarms should take deliberate effort."""
    specs = specs_from_tags(db)
    assert len(specs) >= 15
    by_name = {s.name: s for s in specs}
    assert "fill_temperature.hi_hi" in by_name
    assert by_name["fill_temperature.hi_hi"].deadband == pytest.approx(0.4)
    assert all(s.on_delay > 0 for s in specs)
    assert by_name["fill_temperature.hi_hi"].severity is Severity.CRITICAL
    assert by_name["fill_temperature.hi"].severity is Severity.HIGH
    assert by_name["fill_temperature.hi_hi"].latched is True


def test_alarm_config_file_loads_and_matches_the_tag_map(repo_root, db):
    """The shipped alarm config must reference real tags."""
    from factorylink.alarms import load_alarm_config

    specs = load_alarm_config(str(repo_root / "config" / "alarms.yaml"), db)
    assert len(specs) >= 5
    assert {s.tag for s in specs} <= set(db.names)
    kinds = {s.alarm_type for s in specs}
    assert AlarmType.ROC_HI in kinds
    assert AlarmType.DEVIATION in kinds
    assert AlarmType.DIGITAL in kinds


def test_spec_from_mapping_requires_the_key_fields():
    """A half-written alarm entry should fail loudly at load time."""
    with pytest.raises(ValueError, match="missing"):
        spec_from_mapping({"name": "x", "type": "hi"})


def test_severity_parsing_accepts_names_and_numbers():
    """Config files use words; some HMIs export numbers."""
    assert Severity.parse("critical") is Severity.CRITICAL
    assert Severity.parse("HIGH") is Severity.HIGH
    assert Severity.parse(5) is Severity.CRITICAL
    with pytest.raises(ValueError):
        Severity.parse("catastrophic")
