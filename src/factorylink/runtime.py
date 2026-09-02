"""Wiring: turn a tag database and a driver into a running acquisition stack.

This is the layer the CLI and the demo both use. It owns the order in which
things happen on every scan, which is the part that is easy to get subtly
wrong:

1. poll the due groups;
2. archive the readings (compressed) before anything else looks at them;
3. evaluate alarms against the same values, so the alarm list and the trend
   never disagree about what the process was doing;
4. log alarm transitions into the historian's event table;
5. update the OEE counters and run/stop state from the line state tag.

Doing (3) before (2) would let an alarm reference a value that was never
archived, which is the kind of thing that makes an incident review impossible.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .alarms import AlarmEngine, AlarmSpec, load_alarm_config, specs_from_tags
from .clock import Clock, ManualClock, SystemClock
from .historian import Historian
from .oee import OEECalculator
from .poller import Poller, ScanResult
from .protocols.base import Driver, Quality
from .protocols.simulator import (
    LineState,
    ProcessConfig,
    SimulatedPLC,
    SimulatorDriver,
    bottling_line_tags,
)
from .safety import SAFETY_NOTICE, WriteGuard, WritePolicy
from .tags import TagDatabase

__all__ = [
    "DEFAULT_PERIODS",
    "format_alarm_list",
    "Runtime",
    "build_simulated_runtime",
    "format_scan_table",
    "config_dir",
    "load_tag_database",
]

#: Default poll periods, in seconds, for the bottling line's three groups.
DEFAULT_PERIODS: dict[str, float] = {"fast": 0.5, "normal": 2.0, "slow": 10.0}

#: Line state values that count as producing.
_RUNNING_STATES = {int(LineState.RUNNING)}

#: Human text for each non-running line state, used as the downtime reason
#: when the PLC does not supply a more specific fault code.
_STATE_REASONS = {
    int(LineState.STOPPED): "Line stopped",
    int(LineState.FAULT): "Equipment fault",
    int(LineState.STARVED): "Product starvation",
}

_FAULT_CODE_REASONS = {
    12: "Conveyor jam",
    21: "Motor overload trip",
    33: "Chiller failure",
    41: "Product starvation",
    52: "Air pressure loss",
    64: "Sensor fault",
    71: "Network loss",
}


def config_dir() -> Path:
    """Locate the packaged ``config/`` directory when running from a checkout."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "config"
        if (candidate / "tags_bottling_line.yaml").exists():
            return candidate
    return here.parent


def load_tag_database(path: str | os.PathLike[str] | None = None) -> TagDatabase:
    """Load a tag database from ``path``, or fall back to the built-in map."""
    if path:
        return TagDatabase.load(path)
    candidate = config_dir() / "tags_bottling_line.yaml"
    if candidate.exists():
        try:
            return TagDatabase.load(candidate)
        except Exception:  # noqa: BLE001 - PyYAML missing or file edited badly
            pass
    return bottling_line_tags()


@dataclass
class RuntimeStats:
    """Counters accumulated across a run."""

    scans: int = 0
    readings: int = 0
    bad_readings: int = 0
    rows_archived: int = 0
    alarm_events: int = 0


class Runtime:
    """A complete acquisition stack: poller, historian, alarms, OEE, safety."""

    def __init__(
        self,
        db: TagDatabase,
        drivers: Mapping[str, Driver],
        clock: Clock | None = None,
        periods: Mapping[str, float] | None = None,
        alarm_specs: Iterable[AlarmSpec] | None = None,
        historian: Historian | None = None,
        ideal_cycle_time: float = 0.5,
        write_policy: WritePolicy | None = None,
        state_tag: str = "line_state",
        total_count_tag: str = "product_count",
        reject_count_tag: str = "reject_count",
        fault_code_tag: str = "fault_code",
    ) -> None:
        self.db = db
        self.clock = clock or SystemClock()
        self.poller = Poller(drivers, db, dict(periods or DEFAULT_PERIODS), clock=self.clock)
        self.historian = historian or Historian(clock=self.clock, default_max_interval=60.0)
        self.historian.configure_from_tags(db, tolerance_factor=2.0)
        specs = list(alarm_specs) if alarm_specs is not None else specs_from_tags(db)
        self.alarms = AlarmEngine(specs, clock=self.clock)
        self.oee = OEECalculator(ideal_cycle_time, shift_start=self.clock.now())
        self.guard = WriteGuard(db, write_policy or WritePolicy(), clock=self.clock)
        self.stats = RuntimeStats()
        self.state_tag = state_tag
        self.total_count_tag = total_count_tag
        self.reject_count_tag = reject_count_tag
        self.fault_code_tag = fault_code_tag
        self._last_running: bool | None = None

    # -- the scan cycle -----------------------------------------------------

    def process(self, results: Sequence[ScanResult]) -> None:
        """Fold one or more scan results through history, alarms and OEE."""
        if not results:
            return
        now = self.clock.now()
        merged: dict[str, Any] = {}
        for result in results:
            self.stats.scans += 1
            for reading in result.readings.values():
                self.stats.readings += 1
                if reading.quality is not Quality.GOOD:
                    self.stats.bad_readings += 1
                    continue
                self.stats.rows_archived += self.historian.record_reading(reading)
                merged[reading.tag] = reading.value

        events = self.alarms.update(merged, now=now)
        self.stats.alarm_events += len(events)
        for event in events:
            self.historian.record_event(
                event.alarm, event.kind, event.message, int(event.severity), event.timestamp
            )
        self._update_oee(merged, now)

    def _update_oee(self, values: Mapping[str, Any], now: float) -> None:
        total = values.get(self.total_count_tag)
        rejects = values.get(self.reject_count_tag)
        if total is not None and rejects is not None:
            self.oee.update_counts(int(total), int(rejects))
        state = values.get(self.state_tag)
        if state is None:
            return
        running = int(state) in _RUNNING_STATES
        if running == self._last_running:
            return
        self._last_running = running
        reason = _STATE_REASONS.get(int(state), "Unknown stop")
        code = values.get(self.fault_code_tag)
        if code:
            reason = _FAULT_CODE_REASONS.get(int(code), reason)
        self.oee.record_state(now, running, reason)

    def flush(self) -> int:
        """Archive any pending compressed points and count them in the stats."""
        written = self.historian.flush()
        self.stats.rows_archived += written
        return written

    def poll_once(self) -> list[ScanResult]:
        """Poll every due group and process the results."""
        results = self.poller.poll_once()
        self.process(results)
        return results

    def run(self, duration: float) -> list[ScanResult]:
        """Run the scan loop for ``duration`` seconds, processing as it goes.

        With a :class:`~factorylink.clock.ManualClock` this is instant and
        deterministic.
        """
        deadline = self.clock.now() + duration
        collected: list[ScanResult] = []
        while self.clock.now() < deadline:
            due = self.poller.due_groups()
            if due:
                results = [self.poller.poll_group(group) for group in due]
                self.process(results)
                collected.extend(results)
                continue
            wait = min(self.poller.next_due_time(), deadline) - self.clock.now()
            if wait <= 0:
                break
            self.clock.sleep(wait)
        return collected

    # -- reporting ----------------------------------------------------------

    def values(self) -> dict[str, Any]:
        """Latest engineering value per tag."""
        return {name: r.value for name, r in self.poller.values.items()}

    def oee_result(self) -> Any:
        """Current OEE for the run so far."""
        return self.oee.result(self.clock.now(), close_open_stops=False)

    def summary(self) -> dict[str, Any]:
        """Everything worth printing at the end of a run."""
        return {
            "scans": self.stats.scans,
            "readings": self.stats.readings,
            "bad_readings": self.stats.bad_readings,
            "rows_archived": self.stats.rows_archived,
            "alarm_events": self.stats.alarm_events,
            "history": self.historian.stats(),
            "alarms": self.alarms.summary(),
            "oee": self.oee_result().as_dict(),
            "poller": self.poller.snapshot(),
            "safety_notice": SAFETY_NOTICE,
        }

    def close(self) -> None:
        """Flush and close the historian."""
        self.historian.close()


def build_simulated_runtime(
    clock: Clock | None = None,
    seed: int = 1234,
    db: TagDatabase | None = None,
    periods: Mapping[str, float] | None = None,
    alarm_config: str | os.PathLike[str] | None = None,
    historian_path: str = ":memory:",
    process_config: ProcessConfig | None = None,
    write_policy: WritePolicy | None = None,
) -> tuple[Runtime, SimulatedPLC]:
    """Build a runtime backed by the simulated bottling line.

    Returns the runtime and the PLC, so a caller can inject faults.
    """
    tag_db = db or load_tag_database()
    the_clock = clock or ManualClock()
    plc = SimulatedPLC(config=process_config, seed=seed)
    driver = SimulatorDriver(plc, tag_db, clock=the_clock)
    driver.connect()
    plc.sync_image(tag_db)

    specs = specs_from_tags(tag_db)
    if alarm_config and Path(alarm_config).exists():
        try:
            extra = load_alarm_config(str(alarm_config), tag_db)
        except ValueError:
            extra = []
        known = {s.name for s in specs}
        specs.extend(s for s in extra if s.name not in known)

    historian = Historian(historian_path, clock=the_clock, default_max_interval=60.0)
    runtime = Runtime(
        tag_db,
        {"line1": driver},
        clock=the_clock,
        periods=periods,
        alarm_specs=specs,
        historian=historian,
        ideal_cycle_time=plc.config.ideal_cycle_time,
        write_policy=write_policy,
    )
    return runtime, plc


def format_scan_table(
    runtime: Runtime, groups: Sequence[str] | None = None, width: int = 96
) -> str:
    """Render the current tag values as the ``scan`` command's table."""
    header = (
        f"{'tag':<22}{'address':>14}{'group':>8}{'value':>14}  "
        f"{'unit':<12}{'quality':>8}"
    )
    lines = ["=" * width, f"factorylink scan  t={runtime.clock.now():.1f}s", "=" * width, header,
             "-" * width]
    rows = []
    for name, reading in runtime.poller.values.items():
        tag = runtime.db.get(name)
        if tag is None:
            continue
        if groups and tag.poll_group not in groups:
            continue
        rows.append((tag.poll_group, name, tag, reading))
    rows.sort(key=lambda r: (r[0], r[1]))
    for group, name, tag, reading in rows:
        if isinstance(reading.value, bool):
            value_text = "TRUE" if reading.value else "FALSE"
        elif reading.value is None:
            value_text = "--"
        else:
            value_text = f"{reading.value:.3f}"
        address = f"{tag.area.value}:{tag.address}" + (
            f".{tag.bit}" if tag.bit is not None else ""
        )
        lines.append(
            f"{name:<22}{address:>14}{group:>8}{value_text:>14}  "
            f"{tag.unit:<12}{reading.quality.value:>8}"
        )
    lines.append("-" * width)
    summary = runtime.alarms.summary()
    lines.append(
        f"scans={runtime.stats.scans}  archived={runtime.stats.rows_archived} rows  "
        f"alarms active={summary['active']} unacked={summary['unacked']}"
        + ("  FLOOD" if summary["flood"] else "")
    )
    lines.append("=" * width)
    return "\n".join(lines)


def format_alarm_list(runtime: Runtime, width: int = 96, message_width: int = 30) -> str:
    """Render the alarms needing attention as a plain-text table."""
    summary = runtime.alarms.summary()
    lines = [
        "=" * width,
        f"ALARMS  active={summary['active']}  unacked={summary['unacked']}  "
        f"shelved={summary['shelved']}  rate(10 min)={summary['rate_10min']}"
        + ("  *** FLOOD ***" if summary["flood"] else ""),
        "=" * width,
        f"{'alarm':<30}{'severity':>9}{'state':>13}{'value':>10}  message",
        "-" * width,
    ]
    annunciated = runtime.alarms.annunciated()
    if not annunciated:
        lines.append("no alarms requiring attention")
    for inst in annunciated:
        value = inst.last_value
        value_text = "--" if value is None else (
            str(value) if isinstance(value, bool) else f"{value:.3f}"
        )
        message = inst.spec.describe().replace("\n", " ")
        if len(message) > message_width:
            message = message[: message_width - 3] + "..."
        lines.append(
            f"{inst.spec.name:<30}{inst.spec.severity.name:>9}{inst.state.value:>13}"
            f"{value_text:>10}  {message}"
        )
    lines.append("=" * width)
    return "\n".join(lines)
