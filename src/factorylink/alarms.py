"""Alarm engine with the machinery that stops alarm floods.

The problem this module exists to solve
---------------------------------------
An alarm limit on its own is not an alarm system. Put a bare ``value > 18.0``
on a noisy motor current and the first time the signal sits on the limit you
get an alarm, a return-to-normal, an alarm, a return-to-normal -- once per scan
until the process moves. At a 500 ms scan that is 7200 events an hour from one
tag. Operators stop reading the alarm list, and the one alarm that mattered
scrolls off the top.

This is not hypothetical; it is the standard failure mode that ISA-18.2 and
EEMUA 191 exist to address. EEMUA's guidance is roughly six alarms per hour per
operator in steady state, and more than ten alarms in ten minutes counts as a
flood. A single un-deadbanded analogue alarm can exceed that by itself.

Four mechanisms fix it, and all four are implemented here:

* **Deadband / hysteresis.** Raise at the limit, clear at limit minus a
  deadband. The signal must actually recover before the alarm clears.
* **On-delay.** The condition must hold continuously before the alarm is
  annunciated, so a single noisy sample is not an event.
* **Off-delay.** The condition must be gone continuously before it clears, so
  a signal hovering at the limit does not flicker.
* **Shelving.** An operator can silence a known-bad instrument for a bounded
  time, on the record, instead of ignoring the whole list.

Plus **latching** for alarms that must be acknowledged even after the process
recovers, **severity** so the list can be sorted by consequence, and **flood
detection** so the system can tell you when it has stopped being useful.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Iterable, Mapping, Sequence

from .clock import Clock, SystemClock
from .tags import TagDatabase, TagDef

__all__ = [
    "Severity",
    "AlarmType",
    "AlarmState",
    "AlarmSpec",
    "AlarmEvent",
    "AlarmInstance",
    "AlarmEngine",
    "specs_from_tags",
    "spec_from_mapping",
    "load_alarm_config",
    "alarm_defaults",
]


class Severity(IntEnum):
    """Consequence-ordered severity. Sort the alarm list by this, descending."""

    DIAGNOSTIC = 1
    LOW = 2
    MEDIUM = 3
    HIGH = 4
    CRITICAL = 5

    @classmethod
    def parse(cls, text: str | int) -> "Severity":
        """Parse a severity from config."""
        if isinstance(text, int) and not isinstance(text, bool):
            return cls(max(1, min(5, int(text))))
        key = str(text).strip().upper()
        try:
            return cls[key]
        except KeyError:
            valid = ", ".join(m.name.lower() for m in cls)
            raise ValueError(f"unknown severity {text!r} (expected one of: {valid})") from None


class AlarmType(str, Enum):
    """What kind of comparison raises the alarm."""

    HI_HI = "hi_hi"
    HI = "hi"
    LO = "lo"
    LO_LO = "lo_lo"
    ROC_HI = "roc_hi"
    ROC_LO = "roc_lo"
    DEVIATION = "deviation"
    DIGITAL = "digital"

    @property
    def is_high_side(self) -> bool:
        """True when the alarm trips going up."""
        return self in (AlarmType.HI, AlarmType.HI_HI, AlarmType.ROC_HI)


class AlarmState(str, Enum):
    """ISA-18.2 style alarm state.

    ``NORMAL`` -> ``ACTIVE_UNACK`` when the condition trips.
    ``ACTIVE_UNACK`` -> ``ACTIVE_ACK`` on acknowledgement.
    An active alarm whose condition clears goes to ``RTN_UNACK`` if it is
    latched (it stays in the operator's face until acknowledged) or straight
    back to ``NORMAL`` if it is not.
    """

    NORMAL = "normal"
    ACTIVE_UNACK = "active_unack"
    ACTIVE_ACK = "active_ack"
    RTN_UNACK = "rtn_unack"
    SHELVED = "shelved"

    @property
    def needs_attention(self) -> bool:
        """True for states an operator still has to deal with."""
        return self in (AlarmState.ACTIVE_UNACK, AlarmState.ACTIVE_ACK, AlarmState.RTN_UNACK)


@dataclass(frozen=True)
class AlarmSpec:
    """Configuration for one alarm on one tag."""

    name: str
    tag: str
    alarm_type: AlarmType
    limit: float = 0.0
    deadband: float = 0.0
    on_delay: float = 0.0
    off_delay: float = 0.0
    severity: Severity = Severity.MEDIUM
    latched: bool = False
    enabled: bool = True
    message: str = ""
    #: DEVIATION only: tag holding the setpoint. Falls back to ``limit`` as an
    #: absolute setpoint when unset.
    setpoint_tag: str | None = None
    setpoint: float | None = None
    #: ROC only: window in seconds over which the rate is measured.
    roc_window: float = 5.0
    #: DIGITAL only: the boolean state that constitutes an alarm.
    trigger_state: bool = True

    def __post_init__(self) -> None:
        if self.deadband < 0:
            raise ValueError(f"{self.name}: deadband cannot be negative")
        if self.on_delay < 0 or self.off_delay < 0:
            raise ValueError(f"{self.name}: delays cannot be negative")
        if self.roc_window <= 0:
            raise ValueError(f"{self.name}: roc_window must be positive")

    def describe(self) -> str:
        """One-line human description used in the event text."""
        if self.message:
            return self.message
        if self.alarm_type is AlarmType.DIGITAL:
            return f"{self.tag} is {'set' if self.trigger_state else 'clear'}"
        if self.alarm_type in (AlarmType.ROC_HI, AlarmType.ROC_LO):
            direction = "rising" if self.alarm_type is AlarmType.ROC_HI else "falling"
            return f"{self.tag} {direction} faster than {self.limit}/s"
        if self.alarm_type is AlarmType.DEVIATION:
            return f"{self.tag} deviates from setpoint by more than {self.limit}"
        return f"{self.tag} {self.alarm_type.value.replace('_', '-')} limit {self.limit}"


@dataclass
class AlarmEvent:
    """Something that happened to an alarm. This is the audit trail."""

    timestamp: float
    alarm: str
    tag: str
    kind: str  # raised | cleared | acked | shelved | unshelved | flood
    state: AlarmState
    severity: Severity
    value: float | bool | None
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "timestamp": self.timestamp,
            "alarm": self.alarm,
            "tag": self.tag,
            "kind": self.kind,
            "state": self.state.value,
            "severity": int(self.severity),
            "severity_name": self.severity.name,
            "value": self.value,
            "message": self.message,
        }


@dataclass
class AlarmInstance:
    """Runtime state of one configured alarm."""

    spec: AlarmSpec
    state: AlarmState = AlarmState.NORMAL
    condition: bool = False
    raised_at: float | None = None
    cleared_at: float | None = None
    acked_at: float | None = None
    acked_by: str = ""
    activations: int = 0
    last_value: float | bool | None = None
    shelved_until: float | None = None
    _pending_since: float | None = None
    _clearing_since: float | None = None
    _history: deque = field(default_factory=lambda: deque(maxlen=64))

    @property
    def active(self) -> bool:
        """True while the underlying condition is met."""
        return self.state in (AlarmState.ACTIVE_UNACK, AlarmState.ACTIVE_ACK)

    @property
    def unacknowledged(self) -> bool:
        """True while the alarm still requires an operator acknowledgement."""
        return self.state in (AlarmState.ACTIVE_UNACK, AlarmState.RTN_UNACK)

    @property
    def shelved(self) -> bool:
        """True while the alarm is shelved."""
        return self.state is AlarmState.SHELVED

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form used by the dashboard."""
        return {
            "name": self.spec.name,
            "tag": self.spec.tag,
            "type": self.spec.alarm_type.value,
            "limit": self.spec.limit,
            "state": self.state.value,
            "severity": int(self.spec.severity),
            "severity_name": self.spec.severity.name,
            "active": self.active,
            "unacked": self.unacknowledged,
            "shelved": self.shelved,
            "value": self.last_value,
            "raised_at": self.raised_at,
            "activations": self.activations,
            "message": self.spec.describe(),
        }


class AlarmEngine:
    """Evaluate a set of alarm specs against tag values on every scan."""

    #: EEMUA 191: more than this many new alarms in the flood window means the
    #: operator has stopped being able to respond to them individually.
    FLOOD_COUNT = 10
    FLOOD_WINDOW = 600.0

    def __init__(
        self,
        specs: Iterable[AlarmSpec],
        clock: Clock | None = None,
        flood_count: int | None = None,
        flood_window: float | None = None,
        max_events: int = 2000,
    ) -> None:
        self.clock = clock or SystemClock()
        self.alarms: dict[str, AlarmInstance] = {}
        for spec in specs:
            if spec.name in self.alarms:
                raise ValueError(f"duplicate alarm name {spec.name!r}")
            self.alarms[spec.name] = AlarmInstance(spec)
        self.events: deque[AlarmEvent] = deque(maxlen=max_events)
        self.flood_count = flood_count if flood_count is not None else self.FLOOD_COUNT
        self.flood_window = flood_window if flood_window is not None else self.FLOOD_WINDOW
        self._raise_times: deque[float] = deque(maxlen=4096)
        self._samples: dict[str, deque[tuple[float, float]]] = {}
        self.in_flood = False

    # -- evaluation --------------------------------------------------------

    def update(
        self, values: Mapping[str, float | bool | None], now: float | None = None
    ) -> list[AlarmEvent]:
        """Evaluate every alarm against ``values`` and return new events."""
        stamp = self.clock.now() if now is None else float(now)
        self._record_samples(values, stamp)
        events: list[AlarmEvent] = []
        for instance in self.alarms.values():
            events.extend(self._update_one(instance, values, stamp))
        events.extend(self._check_flood(stamp))
        self.events.extend(events)
        return events

    def _record_samples(self, values: Mapping[str, float | bool | None], now: float) -> None:
        needed = {
            spec.tag
            for spec in (i.spec for i in self.alarms.values())
            if spec.alarm_type in (AlarmType.ROC_HI, AlarmType.ROC_LO)
        }
        for tag in needed:
            value = values.get(tag)
            if value is None or isinstance(value, bool):
                continue
            window = self._samples.setdefault(tag, deque(maxlen=512))
            window.append((now, float(value)))
            horizon = max(
                spec.roc_window
                for spec in (i.spec for i in self.alarms.values())
                if spec.tag == tag and spec.alarm_type in (AlarmType.ROC_HI, AlarmType.ROC_LO)
            )
            while len(window) > 2 and window[0][0] < now - horizon:
                window.popleft()

    def _update_one(
        self, inst: AlarmInstance, values: Mapping[str, float | bool | None], now: float
    ) -> list[AlarmEvent]:
        spec = inst.spec
        events: list[AlarmEvent] = []

        if inst.shelved:
            if inst.shelved_until is not None and now >= inst.shelved_until:
                inst.state = AlarmState.NORMAL
                inst.shelved_until = None
                events.append(self._event(now, inst, "unshelved", inst.last_value))
            else:
                return events

        if not spec.enabled:
            return events

        value = values.get(spec.tag)
        if value is None:
            # Bad quality: hold the alarm state rather than clearing it. A
            # comms failure must never look like the process recovering.
            return events
        inst.last_value = value

        condition = self._evaluate(spec, value, values, now)
        inst.condition = condition

        if condition:
            inst._clearing_since = None
            if inst._pending_since is None:
                inst._pending_since = now
            ready = (now - inst._pending_since) >= spec.on_delay
            if ready and inst.state in (AlarmState.NORMAL, AlarmState.RTN_UNACK):
                inst.state = AlarmState.ACTIVE_UNACK
                inst.raised_at = now
                inst.cleared_at = None
                inst.activations += 1
                self._raise_times.append(now)
                events.append(self._event(now, inst, "raised", value))
        else:
            inst._pending_since = None
            if inst.active:
                if inst._clearing_since is None:
                    inst._clearing_since = now
                if (now - inst._clearing_since) >= spec.off_delay:
                    inst.cleared_at = now
                    inst._clearing_since = None
                    if spec.latched and inst.state is AlarmState.ACTIVE_UNACK:
                        inst.state = AlarmState.RTN_UNACK
                    elif spec.latched and inst.state is AlarmState.ACTIVE_ACK:
                        inst.state = AlarmState.NORMAL
                    else:
                        inst.state = AlarmState.NORMAL
                    events.append(self._event(now, inst, "cleared", value))
            else:
                inst._clearing_since = None
        return events

    def _evaluate(
        self,
        spec: AlarmSpec,
        value: float | bool,
        values: Mapping[str, float | bool | None],
        now: float,
    ) -> bool:
        """Apply hysteresis: a raised alarm needs a bigger move to clear."""
        inst = self.alarms[spec.name]
        currently = inst.condition

        if spec.alarm_type is AlarmType.DIGITAL:
            return bool(value) == spec.trigger_state

        if spec.alarm_type in (AlarmType.ROC_HI, AlarmType.ROC_LO):
            rate = self._rate_of_change(spec, now)
            if rate is None:
                return currently
            if spec.alarm_type is AlarmType.ROC_HI:
                threshold = spec.limit - spec.deadband if currently else spec.limit
                return rate > threshold
            threshold = -spec.limit + spec.deadband if currently else -spec.limit
            return rate < threshold

        numeric = float(value)

        if spec.alarm_type is AlarmType.DEVIATION:
            setpoint = spec.setpoint
            if spec.setpoint_tag is not None:
                sp_value = values.get(spec.setpoint_tag)
                if sp_value is None:
                    return currently
                setpoint = float(sp_value)
            if setpoint is None:
                setpoint = 0.0
            deviation = abs(numeric - setpoint)
            threshold = spec.limit - spec.deadband if currently else spec.limit
            return deviation > threshold

        if spec.alarm_type.is_high_side:
            threshold = spec.limit - spec.deadband if currently else spec.limit
            return numeric > threshold
        threshold = spec.limit + spec.deadband if currently else spec.limit
        return numeric < threshold

    def _rate_of_change(self, spec: AlarmSpec, now: float) -> float | None:
        window = self._samples.get(spec.tag)
        if not window or len(window) < 2:
            return None
        oldest = None
        for stamp, value in window:
            if stamp >= now - spec.roc_window:
                oldest = (stamp, value)
                break
        if oldest is None:
            oldest = window[0]
        newest = window[-1]
        dt = newest[0] - oldest[0]
        if dt <= 0:
            return None
        return (newest[1] - oldest[1]) / dt

    # -- operator actions ---------------------------------------------------

    def acknowledge(self, name: str, by: str = "operator", now: float | None = None) -> AlarmEvent:
        """Acknowledge one alarm. Latched alarms need this to leave the list."""
        stamp = self.clock.now() if now is None else float(now)
        inst = self._get(name)
        inst.acked_at = stamp
        inst.acked_by = by
        if inst.state is AlarmState.ACTIVE_UNACK:
            inst.state = AlarmState.ACTIVE_ACK
        elif inst.state is AlarmState.RTN_UNACK:
            inst.state = AlarmState.NORMAL
        event = self._event(stamp, inst, "acked", inst.last_value, extra=f"by {by}")
        self.events.append(event)
        return event

    def acknowledge_all(self, by: str = "operator", now: float | None = None) -> list[AlarmEvent]:
        """Acknowledge every alarm that currently needs it."""
        return [
            self.acknowledge(name, by, now)
            for name, inst in self.alarms.items()
            if inst.unacknowledged
        ]

    def shelve(self, name: str, duration: float, by: str = "operator") -> AlarmEvent:
        """Suppress an alarm for a bounded time, on the record.

        Bounded is the important word. An unbounded suppression is a disabled
        alarm that nobody remembers disabling.
        """
        if duration <= 0:
            raise ValueError("shelve duration must be positive")
        now = self.clock.now()
        inst = self._get(name)
        inst.state = AlarmState.SHELVED
        inst.shelved_until = now + duration
        inst.condition = False
        inst._pending_since = None
        event = self._event(now, inst, "shelved", inst.last_value, extra=f"{duration:g}s by {by}")
        self.events.append(event)
        return event

    def unshelve(self, name: str) -> AlarmEvent:
        """Return a shelved alarm to service immediately."""
        now = self.clock.now()
        inst = self._get(name)
        inst.state = AlarmState.NORMAL
        inst.shelved_until = None
        event = self._event(now, inst, "unshelved", inst.last_value)
        self.events.append(event)
        return event

    def _get(self, name: str) -> AlarmInstance:
        try:
            return self.alarms[name]
        except KeyError:
            raise KeyError(f"unknown alarm {name!r}") from None

    # -- queries -----------------------------------------------------------

    def active(self) -> list[AlarmInstance]:
        """Alarms whose condition is currently met, worst first."""
        return sorted(
            (i for i in self.alarms.values() if i.active),
            key=lambda i: (-int(i.spec.severity), i.raised_at or 0.0),
        )

    def annunciated(self) -> list[AlarmInstance]:
        """Alarms still needing operator attention, worst first."""
        return sorted(
            (i for i in self.alarms.values() if i.state.needs_attention),
            key=lambda i: (-int(i.spec.severity), i.raised_at or 0.0),
        )

    def worst_severity(self) -> Severity | None:
        """Highest severity currently needing attention."""
        current = self.annunciated()
        return current[0].spec.severity if current else None

    def rate(self, window: float | None = None, now: float | None = None) -> int:
        """New alarms raised in the last ``window`` seconds."""
        stamp = self.clock.now() if now is None else float(now)
        span = self.flood_window if window is None else float(window)
        return sum(1 for t in self._raise_times if t > stamp - span)

    def _check_flood(self, now: float) -> list[AlarmEvent]:
        count = self.rate(now=now)
        flooding = count > self.flood_count
        if flooding and not self.in_flood:
            self.in_flood = True
            return [
                AlarmEvent(
                    now,
                    "__flood__",
                    "",
                    "flood",
                    AlarmState.ACTIVE_UNACK,
                    Severity.HIGH,
                    count,
                    f"alarm flood: {count} alarms in {self.flood_window:g}s "
                    f"(threshold {self.flood_count})",
                )
            ]
        if not flooding and self.in_flood:
            self.in_flood = False
            return [
                AlarmEvent(
                    now,
                    "__flood__",
                    "",
                    "flood_cleared",
                    AlarmState.NORMAL,
                    Severity.LOW,
                    count,
                    "alarm rate back below the flood threshold",
                )
            ]
        return []

    def summary(self) -> dict[str, Any]:
        """Counts by severity plus flood state, for the dashboard banner."""
        by_severity: dict[str, int] = {}
        for inst in self.annunciated():
            by_severity[inst.spec.severity.name] = by_severity.get(inst.spec.severity.name, 0) + 1
        return {
            "configured": len(self.alarms),
            "active": len(self.active()),
            "unacked": sum(1 for i in self.alarms.values() if i.unacknowledged),
            "shelved": sum(1 for i in self.alarms.values() if i.shelved),
            "by_severity": by_severity,
            "rate_10min": self.rate(600.0),
            "flood": self.in_flood,
            "worst": self.worst_severity().name if self.worst_severity() else None,
        }

    def _event(
        self,
        now: float,
        inst: AlarmInstance,
        kind: str,
        value: float | bool | None,
        extra: str = "",
    ) -> AlarmEvent:
        message = inst.spec.describe()
        if extra:
            message = f"{message} ({extra})"
        return AlarmEvent(
            now, inst.spec.name, inst.spec.tag, kind, inst.state, inst.spec.severity, value, message
        )


#: Severity assigned to each limit type when generating specs from a tag map.
_DEFAULT_SEVERITY = {
    AlarmType.HI_HI: Severity.CRITICAL,
    AlarmType.LO_LO: Severity.CRITICAL,
    AlarmType.HI: Severity.HIGH,
    AlarmType.LO: Severity.HIGH,
}


def specs_from_tags(
    db: TagDatabase,
    on_delay: float = 2.0,
    off_delay: float = 2.0,
    latched_severities: Sequence[Severity] = (Severity.CRITICAL,),
) -> list[AlarmSpec]:
    """Generate alarm specs from the limits carried on the tags.

    Every generated alarm gets the tag's configured deadband and a non-zero
    on-delay by default. Producing chattering alarms should take deliberate
    effort, not be the default.
    """
    specs: list[AlarmSpec] = []
    for tag in db:
        limits = (
            (AlarmType.HI_HI, tag.alarm.hi_hi),
            (AlarmType.HI, tag.alarm.hi),
            (AlarmType.LO, tag.alarm.lo),
            (AlarmType.LO_LO, tag.alarm.lo_lo),
        )
        for alarm_type, limit in limits:
            if limit is None:
                continue
            severity = _DEFAULT_SEVERITY[alarm_type]
            specs.append(
                AlarmSpec(
                    name=f"{tag.name}.{alarm_type.value}",
                    tag=tag.name,
                    alarm_type=alarm_type,
                    limit=float(limit),
                    deadband=float(tag.alarm.deadband),
                    on_delay=on_delay,
                    off_delay=off_delay,
                    severity=severity,
                    latched=severity in latched_severities,
                    message=_limit_message(tag, alarm_type, float(limit)),
                )
            )
    return specs


def _limit_message(tag: TagDef, alarm_type: AlarmType, limit: float) -> str:
    label = {
        AlarmType.HI_HI: "very high",
        AlarmType.HI: "high",
        AlarmType.LO: "low",
        AlarmType.LO_LO: "very low",
    }[alarm_type]
    unit = f" {tag.unit}" if tag.unit else ""
    description = tag.description or tag.name
    return f"{description} {label} (limit {limit:g}{unit})"


def spec_from_mapping(data: Mapping[str, Any]) -> AlarmSpec:
    """Build one :class:`AlarmSpec` from a config mapping."""
    required = ("name", "tag", "type")
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise ValueError(f"alarm config is missing {', '.join(missing)}: {dict(data)!r}")
    alarm_type = AlarmType(str(data["type"]).strip().lower())
    return AlarmSpec(
        name=str(data["name"]),
        tag=str(data["tag"]),
        alarm_type=alarm_type,
        limit=float(data.get("limit", 0.0)),
        deadband=float(data.get("deadband", 0.0)),
        on_delay=float(data.get("on_delay", 0.0)),
        off_delay=float(data.get("off_delay", 0.0)),
        severity=Severity.parse(data.get("severity", "medium")),
        latched=bool(data.get("latched", False)),
        enabled=bool(data.get("enabled", True)),
        message=str(data.get("message", "")),
        setpoint_tag=data.get("setpoint_tag"),
        setpoint=None if data.get("setpoint") is None else float(data["setpoint"]),
        roc_window=float(data.get("roc_window", 5.0)),
        trigger_state=bool(data.get("trigger_state", True)),
    )


def load_alarm_config(path: str, db: TagDatabase | None = None) -> list[AlarmSpec]:
    """Load extra alarm specs from a YAML file.

    The file may also override the defaults used when generating limit alarms
    from the tag database::

        defaults:
          on_delay: 3.0
          off_delay: 5.0
        alarms:
          - name: temperature_runaway
            tag: fill_temperature
            type: roc_hi
            limit: 0.25
            roc_window: 20
            severity: high

    Raises:
        ValueError: if an alarm references a tag that is not in ``db``.
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - exercised only without PyYAML
        raise ValueError("PyYAML is required to read an alarm config file") from exc
    with open(path, "r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle) or {}
    specs = [spec_from_mapping(row) for row in (doc.get("alarms") or [])]
    if db is not None:
        unknown = sorted({s.tag for s in specs if s.tag not in db})
        if unknown:
            raise ValueError(f"alarm config references unknown tags: {', '.join(unknown)}")
    return specs


def alarm_defaults(path: str) -> dict[str, Any]:
    """Read the ``defaults:`` section of an alarm config file."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        doc = yaml.safe_load(handle) or {}
    return dict(doc.get("defaults") or {})
