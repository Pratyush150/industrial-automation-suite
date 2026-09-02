"""End-to-end: the whole stack over the simulated line, and the CLI."""

from __future__ import annotations

import subprocess
import sys

import pytest

from factorylink.alarms import AlarmState
from factorylink.protocols.base import Quality
from factorylink.protocols.simulator import Fault, LineState
from factorylink.runtime import (
    build_simulated_runtime,
    format_alarm_list,
    format_scan_table,
    load_tag_database,
)
from factorylink.safety import SafetyError, WritePolicy


@pytest.fixture()
def runtime_with_faults(clock):
    """A 30-minute shift with four injected faults, run in virtual time."""
    runtime, plc = build_simulated_runtime(clock=clock, seed=2026)
    plc.schedule_fault(120.0, Fault.JAM, 90.0)
    plc.schedule_fault(600.0, Fault.CHILLER_FAILURE, 400.0)
    plc.schedule_fault(1200.0, Fault.AIR_LEAK, 150.0)
    plc.schedule_fault(1500.0, Fault.MOTOR_OVERLOAD, 120.0)
    runtime.run(1800.0)
    runtime.historian.flush()
    yield runtime, plc
    runtime.close()


def test_the_whole_stack_runs_offline_and_deterministically(clock):
    """Two runs with the same seed produce identical results."""
    first, _ = build_simulated_runtime(clock=clock, seed=77)
    first.run(300.0)
    summary_a = first.summary()
    first.close()

    from factorylink.clock import ManualClock

    second, _ = build_simulated_runtime(clock=ManualClock(0.0), seed=77)
    second.run(300.0)
    summary_b = second.summary()
    second.close()

    assert summary_a["readings"] == summary_b["readings"]
    assert summary_a["rows_archived"] == summary_b["rows_archived"]
    assert summary_a["oee"]["oee"] == summary_b["oee"]["oee"]


def test_every_tag_is_acquired_with_good_quality(runtime_with_faults):
    """After a shift, every tag has a recent, usable value."""
    runtime, _ = runtime_with_faults
    assert len(runtime.poller.values) == len(runtime.db)
    assert all(r.quality is Quality.GOOD for r in runtime.poller.values.values())
    assert runtime.stats.bad_readings == 0


def test_faults_produce_alarms_and_downtime(runtime_with_faults):
    """The chain from process fault to alarm to OEE loss has to hold."""
    runtime, _ = runtime_with_faults
    assert runtime.stats.alarm_events > 0
    reasons = {row.reason for row in runtime.oee.pareto()}
    assert "Conveyor jam" in reasons
    assert "Chiller failure" in reasons
    assert runtime.oee.tracker.unplanned() > 600.0


def test_oee_reflects_the_injected_downtime(runtime_with_faults):
    """Availability must be well below 1 after 12 minutes of stoppage."""
    runtime, _ = runtime_with_faults
    result = runtime.oee_result()
    assert 0.5 < result.availability < 0.95
    assert result.quality > 0.9
    assert result.oee == pytest.approx(
        result.availability * result.performance * result.quality
    )
    assert result.total_count > 2000


def test_the_chiller_failure_shows_up_as_a_temperature_alarm(runtime_with_faults):
    """A slow process fault must reach the alarm list, not just the trend."""
    runtime, _ = runtime_with_faults
    names = {inst.spec.name for inst in runtime.alarms.alarms.values() if inst.activations}
    assert "fill_temperature.hi" in names or "temperature_runaway" in names


def test_history_is_compressed_but_keeps_the_events(runtime_with_faults):
    """Compression must not erase the fault: the peaks are still in there."""
    runtime, _ = runtime_with_faults
    stats = runtime.historian.stats()
    assert stats["ratio"] < 0.8
    temperatures = [s.value for s in runtime.historian.query("fill_temperature")]
    assert max(temperatures) > 8.0
    currents = [s.value for s in runtime.historian.query("motor_current")]
    assert max(currents) > 24.0


def test_alarm_events_are_written_to_the_historian(runtime_with_faults):
    """An incident review needs the events next to the trend."""
    runtime, _ = runtime_with_faults
    events = runtime.historian.events(limit=500)
    assert len(events) > 0
    assert {e["kind"] for e in events} & {"raised", "cleared"}


def test_latched_alarms_survive_until_acknowledged(runtime_with_faults):
    """The jam cleared long ago; the critical alarm is still on the list."""
    runtime, _ = runtime_with_faults
    latched = [
        inst
        for inst in runtime.alarms.annunciated()
        if inst.state is AlarmState.RTN_UNACK
    ]
    assert latched
    runtime.alarms.acknowledge_all(by="test")
    assert all(i.state is not AlarmState.RTN_UNACK for i in runtime.alarms.alarms.values())


def test_the_write_path_stays_shut_by_default(runtime_with_faults):
    """A full runtime is read-only until somebody changes the policy."""
    runtime, _ = runtime_with_faults
    with pytest.raises(SafetyError):
        runtime.guard.check("conveyor_speed_sp", 20.0)
    runtime.guard.policy = WritePolicy().allowing({"conveyor_speed_sp"})
    approved = runtime.guard.check("conveyor_speed_sp", 20.0)
    assert approved.value == 20.0


def test_a_write_through_the_guard_changes_the_process(clock):
    """Guard -> driver -> simulated PLC, end to end."""
    runtime, plc = build_simulated_runtime(clock=clock, seed=3)
    runtime.run(60.0)
    runtime.guard.policy = WritePolicy().allowing({"conveyor_speed_sp"})
    driver = runtime.poller.drivers["line1"]
    runtime.guard.apply(driver, "conveyor_speed_sp", 18.0, operator="test")
    runtime.run(120.0)
    assert runtime.poller.values["conveyor_speed"].value == pytest.approx(18.0, rel=0.05)
    runtime.close()


def test_scan_table_and_alarm_list_render(runtime_with_faults):
    """The CLI output is a deliverable; its shape is pinned."""
    runtime, _ = runtime_with_faults
    table = format_scan_table(runtime)
    assert "factorylink scan" in table
    assert "conveyor_speed" in table
    assert "holding:0" in table
    assert table.count("\n") > 35
    alarms = format_alarm_list(runtime)
    assert "ALARMS" in alarms


def test_the_shipped_config_drives_the_whole_stack(clock, repo_root):
    """Load the YAML tag map and alarm config from disk and run on them."""
    db = load_tag_database(repo_root / "config" / "tags_bottling_line.yaml")
    runtime, plc = build_simulated_runtime(
        clock=clock, db=db, alarm_config=repo_root / "config" / "alarms.yaml"
    )
    assert len(runtime.alarms.alarms) > len(db.groups)
    plc.schedule_fault(30.0, Fault.JAM, 30.0)
    runtime.run(300.0)
    assert runtime.alarms.alarms["conveyor_jam"].activations == 1
    assert plc.state is LineState.RUNNING
    runtime.close()


# -- CLI -------------------------------------------------------------------


def run_cli(repo_root, *args, timeout: int = 240) -> subprocess.CompletedProcess:
    """Invoke tools/factorylink in a subprocess."""
    return subprocess.run(
        [sys.executable, str(repo_root / "tools" / "factorylink"), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(repo_root),
    )


def test_cli_demo_runs_end_to_end(repo_root):
    """The one command a reader will actually type."""
    result = run_cli(repo_root, "--demo", "--duration", "600")
    assert result.returncode == 0, result.stderr
    for expected in (
        "tag database",
        "factorylink scan",
        "ALARMS",
        "OEE REPORT",
        "DOWNTIME PARETO",
        "HISTORIAN",
        "SAFETY / WRITE PATH",
        "NOT a safety instrumented system",
    ):
        assert expected in result.stdout


def test_cli_dump_tags_prints_the_read_plan(repo_root):
    """dump-tags --plan is the coalescing evidence."""
    result = run_cli(repo_root, "dump-tags", "--plan")
    assert result.returncode == 0, result.stderr
    assert "TAG DATABASE  38 tags" in result.stdout
    assert "COALESCED READ PLAN" in result.stdout
    assert "21 tags -> 2 request(s)" in result.stdout


def test_cli_dump_tags_yaml_round_trips(repo_root, db):
    """The YAML the CLI emits must load back into the same database."""
    from factorylink.tags import TagDatabase

    result = run_cli(repo_root, "dump-tags", "--format", "yaml")
    assert result.returncode == 0, result.stderr
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tags.yaml"
        path.write_text(result.stdout, encoding="utf-8")
        assert TagDatabase.load(path).names == db.names


def test_cli_oee_and_history_and_scan(repo_root):
    """The remaining subcommands all run against the simulator."""
    oee = run_cli(repo_root, "oee", "--duration", "600", "--fault", "jam@100:60")
    assert oee.returncode == 0, oee.stderr
    assert "OEE REPORT" in oee.stdout
    assert "Conveyor jam" in oee.stdout

    history = run_cli(repo_root, "history", "--tag", "tank_level", "--duration", "600")
    assert history.returncode == 0, history.stderr
    assert "HISTORY  tank_level" in history.stdout
    assert "compression ratio" in history.stdout

    scan = run_cli(repo_root, "scan", "--duration", "30")
    assert scan.returncode == 0, scan.stderr
    assert "factorylink scan" in scan.stdout


def test_cli_reports_an_unknown_tag(repo_root):
    """A typo must exit non-zero with a useful message."""
    result = run_cli(repo_root, "history", "--tag", "nope", "--duration", "10")
    assert result.returncode == 2
    assert "unknown tag" in result.stderr


def test_cli_rejects_a_malformed_fault_spec(repo_root):
    """--fault jam is missing the time; say so."""
    result = run_cli(repo_root, "simulate", "--fault", "jam")
    assert result.returncode != 0
    assert "name@start" in result.stderr
