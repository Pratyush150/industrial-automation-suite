"""OEE: Overall Equipment Effectiveness, plus downtime reason tracking.

OEE is the one number a plant manager already trusts, so it is worth computing
exactly the way the standard does rather than inventing a variant.

    Availability = run time / planned production time
    Performance  = (ideal cycle time x total count) / run time
    Quality      = good count / total count
    OEE          = Availability x Performance x Quality

Three things that are commonly got wrong, and are handled here:

**Planned production time excludes planned downtime.** Scheduled changeovers,
planned maintenance and unstaffed shifts are not availability losses. Counting
them makes OEE look terrible for reasons nobody can act on. This module tracks
each stop with a category so planned time can be subtracted properly.

**Performance above 100% means the ideal cycle time is wrong.** It does not
mean the line beat physics. The result carries a
:attr:`OEEResult.performance_clamped` flag, and the raw value is preserved, so
the error is visible rather than hidden by a silent clamp.

**Total count includes rejects.** Performance is measured on everything the
machine made, including what it then threw away; the throwing-away is the
quality loss. Using good count in both places double-counts it.

The downtime Pareto answers the follow-up question. OEE tells you there is a
20% availability loss; the Pareto tells you that 70% of it is one changeover
and a jam, which is the thing you can actually go and fix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

__all__ = [
    "DowntimeEvent",
    "OEEResult",
    "ParetoEntry",
    "compute_oee",
    "DowntimeTracker",
    "OEECalculator",
    "WORLD_CLASS",
]

#: Widely quoted "world class" reference values. Included as context for the
#: report text, not as a claim about any particular line.
WORLD_CLASS = {"availability": 0.90, "performance": 0.95, "quality": 0.999, "oee": 0.85}

PLANNED = "planned"
UNPLANNED = "unplanned"


@dataclass
class DowntimeEvent:
    """One period during which the equipment was not producing."""

    start: float
    end: float | None
    reason: str
    category: str = UNPLANNED

    @property
    def duration(self) -> float:
        """Seconds of downtime. An open event contributes nothing until closed."""
        if self.end is None:
            return 0.0
        return max(0.0, float(self.end) - float(self.start))

    @property
    def is_planned(self) -> bool:
        """True for stops that should be excluded from planned production time."""
        return self.category == PLANNED

    def close(self, end: float) -> None:
        """Close an open event at ``end``."""
        if self.end is None:
            self.end = float(end)

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "start": self.start,
            "end": self.end,
            "reason": self.reason,
            "category": self.category,
            "duration": round(self.duration, 3),
        }


@dataclass
class ParetoEntry:
    """One row of a downtime Pareto."""

    reason: str
    events: int
    seconds: float
    share: float
    cumulative: float

    @property
    def minutes(self) -> float:
        """Downtime in minutes."""
        return self.seconds / 60.0

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "reason": self.reason,
            "events": self.events,
            "seconds": round(self.seconds, 3),
            "minutes": round(self.minutes, 3),
            "share": round(self.share, 4),
            "cumulative": round(self.cumulative, 4),
        }


@dataclass
class OEEResult:
    """The three factors, the composite, and the inputs they came from."""

    availability: float
    performance: float
    quality: float
    oee: float
    planned_time: float
    run_time: float
    downtime: float
    planned_downtime: float
    total_count: int
    good_count: int
    reject_count: int
    ideal_cycle_time: float
    performance_raw: float
    performance_clamped: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def teep_hint(self) -> float:
        """OEE scaled by the fraction of calendar time that was scheduled.

        Only meaningful when ``planned_downtime`` covers unscheduled time.
        """
        total = self.planned_time + self.planned_downtime
        if total <= 0:
            return 0.0
        return self.oee * (self.planned_time / total)

    @property
    def losses(self) -> dict[str, float]:
        """Fraction of theoretical output lost to each of the three factors."""
        return {
            "availability": 1.0 - self.availability,
            "performance": self.availability * (1.0 - self.performance),
            "quality": self.availability * self.performance * (1.0 - self.quality),
        }

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form used by the CLI and dashboard."""
        return {
            "availability": round(self.availability, 6),
            "performance": round(self.performance, 6),
            "quality": round(self.quality, 6),
            "oee": round(self.oee, 6),
            "planned_time": round(self.planned_time, 3),
            "run_time": round(self.run_time, 3),
            "downtime": round(self.downtime, 3),
            "planned_downtime": round(self.planned_downtime, 3),
            "total_count": self.total_count,
            "good_count": self.good_count,
            "reject_count": self.reject_count,
            "ideal_cycle_time": self.ideal_cycle_time,
            "performance_raw": round(self.performance_raw, 6),
            "performance_clamped": self.performance_clamped,
            "notes": list(self.notes),
        }

    def format_report(self, width: int = 66) -> str:
        """Render the classic OEE breakdown as plain text."""
        bar_width = 24

        def bar(value: float) -> str:
            filled = int(round(max(0.0, min(1.0, value)) * bar_width))
            return "#" * filled + "." * (bar_width - filled)

        lines = [
            "=" * width,
            "OEE REPORT",
            "=" * width,
            f"planned production time : {self.planned_time / 60.0:>10.1f} min",
            f"  run time              : {self.run_time / 60.0:>10.1f} min",
            f"  unplanned downtime    : {self.downtime / 60.0:>10.1f} min",
            f"  planned downtime      : {self.planned_downtime / 60.0:>10.1f} min  (excluded)",
            f"ideal cycle time        : {self.ideal_cycle_time:>10.3f} s/unit",
            f"total count             : {self.total_count:>10d}",
            f"good count              : {self.good_count:>10d}",
            f"reject count            : {self.reject_count:>10d}",
            "-" * width,
            f"Availability {self.availability * 100:6.2f}%  [{bar(self.availability)}]",
            f"Performance  {self.performance * 100:6.2f}%  [{bar(self.performance)}]",
            f"Quality      {self.quality * 100:6.2f}%  [{bar(self.quality)}]",
            "-" * width,
            f"OEE          {self.oee * 100:6.2f}%  [{bar(self.oee)}]",
            "=" * width,
        ]
        for note in self.notes:
            lines.append(f"note: {note}")
        return "\n".join(lines)


def compute_oee(
    planned_time: float,
    downtime: float,
    ideal_cycle_time: float,
    total_count: int,
    reject_count: int = 0,
    planned_downtime: float = 0.0,
    clamp_performance: bool = True,
) -> OEEResult:
    """Compute OEE from the five numbers it actually needs.

    Args:
        planned_time: seconds the equipment was scheduled to produce,
            already excluding planned downtime.
        downtime: seconds of unplanned stoppage inside ``planned_time``.
        ideal_cycle_time: seconds per unit at the design rate.
        total_count: everything produced, rejects included.
        reject_count: units that failed inspection.
        planned_downtime: seconds excluded from ``planned_time``; recorded
            for reporting only.
        clamp_performance: cap performance at 1.0 while keeping the raw value.

    Raises:
        ValueError: on negative inputs or a non-positive ideal cycle time.
    """
    if planned_time < 0 or downtime < 0 or planned_downtime < 0:
        raise ValueError("times cannot be negative")
    if ideal_cycle_time <= 0:
        raise ValueError("ideal cycle time must be positive")
    if total_count < 0 or reject_count < 0:
        raise ValueError("counts cannot be negative")
    if reject_count > total_count:
        raise ValueError(f"reject count {reject_count} exceeds total count {total_count}")
    if downtime > planned_time:
        raise ValueError(f"downtime {downtime} exceeds planned time {planned_time}")

    notes: list[str] = []
    run_time = planned_time - downtime
    good_count = total_count - reject_count

    availability = run_time / planned_time if planned_time > 0 else 0.0
    performance_raw = (ideal_cycle_time * total_count / run_time) if run_time > 0 else 0.0
    quality = (good_count / total_count) if total_count > 0 else 0.0

    performance = performance_raw
    clamped = False
    if clamp_performance and performance_raw > 1.0:
        performance = 1.0
        clamped = True
        notes.append(
            f"performance came out at {performance_raw * 100:.1f}%, which means the "
            f"ideal cycle time of {ideal_cycle_time:g}s is too slow, the run time is "
            f"under-counted, or the counter is double-counting. Fix the input rather "
            f"than trusting the clamp."
        )
    if planned_time == 0:
        notes.append("planned production time is zero; every factor is reported as 0.")
    if total_count == 0 and run_time > 0:
        notes.append("no production counted during run time; performance and quality are 0.")

    return OEEResult(
        availability=availability,
        performance=performance,
        quality=quality,
        oee=availability * performance * quality,
        planned_time=float(planned_time),
        run_time=float(run_time),
        downtime=float(downtime),
        planned_downtime=float(planned_downtime),
        total_count=int(total_count),
        good_count=int(good_count),
        reject_count=int(reject_count),
        ideal_cycle_time=float(ideal_cycle_time),
        performance_raw=performance_raw,
        performance_clamped=clamped,
        notes=notes,
    )


class DowntimeTracker:
    """Collect stop events and turn them into a Pareto of causes."""

    def __init__(self, events: Iterable[DowntimeEvent] | None = None) -> None:
        self.events: list[DowntimeEvent] = list(events or [])

    def start_stop(self, timestamp: float, reason: str, category: str = UNPLANNED) -> DowntimeEvent:
        """Open a downtime event. A second call while one is open is ignored."""
        if self.open_event is not None:
            return self.open_event
        event = DowntimeEvent(float(timestamp), None, reason, category)
        self.events.append(event)
        return event

    def end_stop(self, timestamp: float) -> DowntimeEvent | None:
        """Close the open downtime event, if any."""
        event = self.open_event
        if event is not None:
            event.close(timestamp)
        return event

    @property
    def open_event(self) -> DowntimeEvent | None:
        """The currently open downtime event, if the line is stopped."""
        for event in reversed(self.events):
            if event.end is None:
                return event
        return None

    def close_all(self, timestamp: float) -> None:
        """Close every open event at ``timestamp`` (end of shift)."""
        for event in self.events:
            event.close(timestamp)

    def total(self, category: str | None = None) -> float:
        """Total downtime seconds, optionally for one category."""
        return sum(
            e.duration for e in self.events if category is None or e.category == category
        )

    def unplanned(self) -> float:
        """Total unplanned downtime in seconds."""
        return self.total(UNPLANNED)

    def planned(self) -> float:
        """Total planned downtime in seconds."""
        return self.total(PLANNED)

    def pareto(self, category: str | None = UNPLANNED, top: int | None = None) -> list[ParetoEntry]:
        """Downtime grouped by reason, longest first, with cumulative share."""
        totals: dict[str, list[float]] = {}
        for event in self.events:
            if category is not None and event.category != category:
                continue
            if event.duration <= 0:
                continue
            bucket = totals.setdefault(event.reason, [0.0, 0.0])
            bucket[0] += event.duration
            bucket[1] += 1
        grand = sum(v[0] for v in totals.values())
        rows = sorted(totals.items(), key=lambda kv: (-kv[1][0], kv[0]))
        out: list[ParetoEntry] = []
        cumulative = 0.0
        for reason, (seconds, count) in rows:
            share = seconds / grand if grand else 0.0
            cumulative += share
            out.append(ParetoEntry(reason, int(count), seconds, share, cumulative))
        return out[:top] if top else out

    def format_pareto(self, category: str | None = UNPLANNED, width: int = 66) -> str:
        """Render the Pareto as a plain-text table with a bar column."""
        rows = self.pareto(category)
        label = category or "all"
        lines = [
            "=" * width,
            f"DOWNTIME PARETO ({label})",
            "=" * width,
            f"{'reason':<26}{'events':>7}{'minutes':>10}{'share':>8}  {'cum':>6}",
            "-" * width,
        ]
        if not rows:
            lines.append("no downtime recorded")
        for row in rows:
            lines.append(
                f"{row.reason[:26]:<26}{row.events:>7}{row.minutes:>10.2f}"
                f"{row.share * 100:>7.1f}%{row.cumulative * 100:>7.1f}%"
            )
        lines.append("=" * width)
        return "\n".join(lines)


class OEECalculator:
    """Build an :class:`OEEResult` from run/stop events and counters.

    This is the layer that sits between the simulated (or real) line and
    :func:`compute_oee`: it owns the downtime tracker and the shift window.
    """

    def __init__(
        self,
        ideal_cycle_time: float,
        shift_start: float = 0.0,
        tracker: DowntimeTracker | None = None,
    ) -> None:
        if ideal_cycle_time <= 0:
            raise ValueError("ideal cycle time must be positive")
        self.ideal_cycle_time = float(ideal_cycle_time)
        self.shift_start = float(shift_start)
        self.tracker = tracker or DowntimeTracker()
        self.total_count = 0
        self.reject_count = 0

    def update_counts(self, total_count: int, reject_count: int) -> None:
        """Set the absolute production counters read from the PLC."""
        if total_count < 0 or reject_count < 0:
            raise ValueError("counts cannot be negative")
        self.total_count = int(total_count)
        self.reject_count = int(reject_count)

    def record_state(self, timestamp: float, running: bool, reason: str = "Unknown stop",
                     category: str = UNPLANNED) -> None:
        """Feed a run/stop transition; opens or closes a downtime event."""
        if running:
            self.tracker.end_stop(timestamp)
        else:
            self.tracker.start_stop(timestamp, reason, category)

    def load_events(self, events: Iterable[Mapping[str, Any] | DowntimeEvent]) -> None:
        """Bulk-load downtime events from dicts or dataclasses."""
        for item in events:
            if isinstance(item, DowntimeEvent):
                self.tracker.events.append(item)
                continue
            self.tracker.events.append(
                DowntimeEvent(
                    float(item["start"]),
                    None if item.get("end") is None else float(item["end"]),
                    str(item.get("reason", "Unknown stop")),
                    str(item.get("category", UNPLANNED)),
                )
            )

    def result(self, now: float, close_open_stops: bool = True) -> OEEResult:
        """Compute OEE for the shift window ending at ``now``."""
        if close_open_stops:
            self.tracker.close_all(now)
        elapsed = max(0.0, now - self.shift_start)
        planned_downtime = self.tracker.planned()
        planned_time = max(0.0, elapsed - planned_downtime)
        return compute_oee(
            planned_time=planned_time,
            downtime=min(self.tracker.unplanned(), planned_time),
            ideal_cycle_time=self.ideal_cycle_time,
            total_count=self.total_count,
            reject_count=self.reject_count,
            planned_downtime=planned_downtime,
        )

    def pareto(self, top: int | None = None) -> list[ParetoEntry]:
        """Downtime Pareto for unplanned stops."""
        return self.tracker.pareto(top=top)
