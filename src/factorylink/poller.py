"""The acquisition loop: poll groups, register coalescing and connection health.

Three ideas do most of the work here.

**Poll groups.** Not every tag deserves the same rate. A conveyor speed is
worth reading every 500 ms; the ambient temperature is not worth reading more
than once every 10 s. Grouping tags by rate is the single biggest reduction in
PLC load available, and it costs nothing.

**Register coalescing.** Thirty tags scattered across forty registers do not
need thirty requests. Merging them into contiguous blocks turns a scan that
takes thirty round trips into one that takes two. On a 20 ms round trip that
is 600 ms versus 40 ms, per scan, forever. The rules the merge must respect:

* never merge across devices -- different units, different sockets;
* never merge across register areas -- holding 40 and coil 40 are unrelated;
* never exceed the protocol limit (125 registers, 2000 bits);
* only bridge a gap when reading the padding is cheaper than a second round
  trip, which is what ``max_gap`` expresses.

With ``max_gap`` unbounded, the greedy left-to-right merge implemented here
produces the provably minimum number of blocks -- see
``tests/test_poller_coalescing.py``, which checks it against a brute-force
search.

**Staggering.** If four groups all have a period that divides 10 s, they all
come due at t=0, t=10, t=20 and the PLC sees a burst followed by silence.
Offsetting each group's phase spreads the same traffic evenly. Bursty polling
is a common cause of "the HMI goes unresponsive every ten seconds".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .clock import Clock, SystemClock
from .datatypes import MAX_READ_COUNT, RegisterArea
from .protocols.base import Driver, DriverError, Quality, Reading
from .tags import TagDatabase, TagDef

__all__ = [
    "ReadBlock",
    "coalesce_blocks",
    "PollGroup",
    "ConnectionHealth",
    "ScanResult",
    "Poller",
]


@dataclass(frozen=True)
class ReadBlock:
    """One contiguous protocol read covering one or more tags."""

    device: str
    area: RegisterArea
    start: int
    count: int
    tags: tuple[TagDef, ...]

    @property
    def end(self) -> int:
        """Last address inclusive."""
        return self.start + self.count - 1

    @property
    def wasted(self) -> int:
        """Registers read that no tag actually needs.

        Counts distinct addresses, so five status bits sharing one word count
        as one used register rather than five.
        """
        used: set[int] = set()
        for tag in self.tags:
            used.update(range(tag.address, tag.end_address + 1))
        return self.count - len(used)

    def __str__(self) -> str:
        return (
            f"{self.device}/{self.area.value}[{self.start}..{self.end}] "
            f"{self.count} regs, {len(self.tags)} tags"
        )


def coalesce_blocks(
    tags: Iterable[TagDef],
    max_gap: int = 8,
    max_count: Mapping[RegisterArea, int] | None = None,
) -> list[ReadBlock]:
    """Merge tag addresses into the fewest legal protocol reads.

    Args:
        tags: tags to cover. May span several devices and areas.
        max_gap: largest run of unused registers to read through rather than
            starting a new request. 0 means "contiguous only".
        max_count: per-area read limits; defaults to the protocol maxima.

    Returns:
        Blocks sorted by device, then area, then start address. Every tag
        appears in exactly one block.

    Raises:
        ValueError: if ``max_gap`` is negative, or a single tag is wider than
            its area's read limit.
    """
    if max_gap < 0:
        raise ValueError("max_gap cannot be negative")
    limits = dict(MAX_READ_COUNT)
    if max_count:
        limits.update(max_count)

    spaces: dict[tuple[str, RegisterArea], list[TagDef]] = {}
    for tag in tags:
        spaces.setdefault((tag.device, tag.area), []).append(tag)

    blocks: list[ReadBlock] = []
    for (device, area), space_tags in sorted(spaces.items(), key=lambda kv: (kv[0][0], kv[0][1].value)):
        limit = limits[area]
        for tag in space_tags:
            if tag.register_count > limit:
                raise ValueError(
                    f"{tag.name} needs {tag.register_count} units but the "
                    f"{area.value} read limit is {limit}"
                )
        ordered = sorted(space_tags, key=lambda t: (t.address, t.end_address))
        current: list[TagDef] = []
        start = 0
        end = -1
        for tag in ordered:
            if not current:
                current, start, end = [tag], tag.address, tag.end_address
                continue
            new_end = max(end, tag.end_address)
            gap = tag.address - end - 1
            if gap <= max_gap and (new_end - start + 1) <= limit:
                current.append(tag)
                end = new_end
                continue
            blocks.append(ReadBlock(device, area, start, end - start + 1, tuple(current)))
            current, start, end = [tag], tag.address, tag.end_address
        if current:
            blocks.append(ReadBlock(device, area, start, end - start + 1, tuple(current)))
    return blocks


@dataclass
class PollGroup:
    """A set of tags read together at a fixed period."""

    name: str
    period: float
    tags: tuple[TagDef, ...] = ()
    phase: float = 0.0
    enabled: bool = True

    next_due: float = 0.0
    last_start: float = 0.0
    last_duration: float = 0.0
    scans: int = 0
    overruns: int = 0
    failures: int = 0
    max_duration: float = 0.0

    def __post_init__(self) -> None:
        if self.period <= 0:
            raise ValueError(f"poll group {self.name!r} needs a positive period")

    @property
    def duty(self) -> float:
        """Fraction of the period the last scan consumed."""
        return self.last_duration / self.period if self.period else 0.0

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly summary for the dashboard and CLI."""
        return {
            "name": self.name,
            "period": self.period,
            "tags": len(self.tags),
            "scans": self.scans,
            "overruns": self.overruns,
            "failures": self.failures,
            "last_duration": round(self.last_duration, 6),
            "max_duration": round(self.max_duration, 6),
            "duty": round(self.duty, 4),
        }


@dataclass
class ConnectionHealth:
    """Per-device connection state with exponential reconnect backoff.

    A device that has gone away must not be retried on every scan: that turns
    one dead PLC into a scan loop that spends all its time in connect
    timeouts, and takes the healthy devices down with it.
    """

    device: str
    connected: bool = False
    consecutive_failures: int = 0
    total_failures: int = 0
    reconnects: int = 0
    last_error: str | None = None
    last_success: float | None = None
    next_retry_at: float = 0.0
    base_backoff: float = 0.5
    max_backoff: float = 30.0

    def backoff(self) -> float:
        """Seconds to wait before the next reconnect attempt."""
        if self.consecutive_failures <= 0:
            return 0.0
        delay = self.base_backoff * (2 ** (self.consecutive_failures - 1))
        return min(self.max_backoff, delay)

    def record_success(self, now: float) -> None:
        """Mark a successful read or connect."""
        was_down = not self.connected
        self.connected = True
        self.consecutive_failures = 0
        self.next_retry_at = now
        self.last_success = now
        self.last_error = None
        if was_down:
            self.reconnects += 1

    def record_failure(self, now: float, error: str) -> None:
        """Mark a failure and schedule the next retry."""
        self.connected = False
        self.consecutive_failures += 1
        self.total_failures += 1
        self.last_error = error
        self.next_retry_at = now + self.backoff()

    def may_retry(self, now: float) -> bool:
        """True when the backoff has elapsed."""
        return now >= self.next_retry_at

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly summary."""
        return {
            "device": self.device,
            "connected": self.connected,
            "consecutive_failures": self.consecutive_failures,
            "total_failures": self.total_failures,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
            "backoff": round(self.backoff(), 3),
        }


@dataclass
class ScanResult:
    """What one scan of one poll group produced."""

    group: str
    started: float
    duration: float
    readings: dict[str, Reading] = field(default_factory=dict)
    blocks: int = 0
    overrun: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def good(self) -> int:
        """Number of GOOD-quality readings."""
        return sum(1 for r in self.readings.values() if r.quality is Quality.GOOD)


class Poller:
    """Scan loop over one or more drivers.

    The loop is synchronous and single-threaded on purpose. Industrial scan
    loops are easier to reason about, easier to test and easier to hand over
    when there is exactly one thread deciding what happens next.
    """

    def __init__(
        self,
        drivers: Mapping[str, Driver],
        db: TagDatabase,
        periods: Mapping[str, float] | None = None,
        clock: Clock | None = None,
        max_gap: int = 8,
        stagger: bool = True,
        overrun_ratio: float = 0.8,
    ) -> None:
        self.drivers = dict(drivers)
        self.db = db
        self.clock = clock or SystemClock()
        self.max_gap = max_gap
        self.overrun_ratio = overrun_ratio
        self.health = {name: ConnectionHealth(name) for name in self.drivers}
        self.values: dict[str, Reading] = {}
        self.scan_count = 0
        self.on_readings: list[Any] = []

        periods = dict(periods or {})
        self.groups: dict[str, PollGroup] = {}
        for group_name in db.groups:
            tags = tuple(db.by_group(group_name))
            period = float(periods.get(group_name, 1.0))
            self.groups[group_name] = PollGroup(group_name, period, tags)
        for name, period in periods.items():
            if name not in self.groups:
                self.groups[name] = PollGroup(name, float(period), ())

        self._blocks: dict[str, list[ReadBlock]] = {
            name: coalesce_blocks(group.tags, max_gap=self.max_gap)
            for name, group in self.groups.items()
        }
        if stagger:
            self.apply_stagger()
        start = self.clock.now()
        for group in self.groups.values():
            group.next_due = start + group.phase

    # -- planning ---------------------------------------------------------

    def blocks_for(self, group: str) -> list[ReadBlock]:
        """Coalesced read blocks for one poll group."""
        return list(self._blocks[group])

    def plan(self) -> list[ReadBlock]:
        """Every read block across every group, for `dump-tags --plan`."""
        out: list[ReadBlock] = []
        for name in self.groups:
            out.extend(self._blocks[name])
        return out

    def apply_stagger(self) -> None:
        """Spread groups that share a period so they do not all fire together.

        Groups are bucketed by period; within a bucket the phases are spaced
        evenly across that period.
        """
        buckets: dict[float, list[PollGroup]] = {}
        for group in self.groups.values():
            buckets.setdefault(group.period, []).append(group)
        for period, members in buckets.items():
            members.sort(key=lambda g: g.name)
            for index, group in enumerate(members):
                group.phase = period * index / max(1, len(members))

    # -- execution --------------------------------------------------------

    def due_groups(self, now: float | None = None) -> list[PollGroup]:
        """Groups whose next scan time has arrived, earliest first."""
        stamp = self.clock.now() if now is None else now
        due = [g for g in self.groups.values() if g.enabled and g.tags and stamp >= g.next_due]
        due.sort(key=lambda g: (g.next_due, g.name))
        return due

    def next_due_time(self) -> float:
        """Absolute time of the next scan across all groups."""
        candidates = [g.next_due for g in self.groups.values() if g.enabled and g.tags]
        return min(candidates) if candidates else math.inf

    def poll_group(self, group: PollGroup) -> ScanResult:
        """Run one scan of one group and fold the results into ``values``."""
        started = self.clock.now()
        group.last_start = started
        readings: dict[str, Reading] = {}
        errors: list[str] = []
        blocks = self._blocks[group.name]

        by_device: dict[str, list[ReadBlock]] = {}
        for block in blocks:
            by_device.setdefault(block.device, []).append(block)

        for device, device_blocks in by_device.items():
            driver = self.drivers.get(device)
            health = self.health.setdefault(device, ConnectionHealth(device))
            tags = [t for b in device_blocks for t in b.tags]
            if driver is None:
                message = f"no driver configured for device {device!r}"
                errors.append(message)
                health.record_failure(started, message)
                readings.update(_bad(tags, started, message))
                continue
            if not self._ensure_connected(driver, health, started):
                message = health.last_error or "device offline"
                errors.append(f"{device}: {message}")
                readings.update(_bad(tags, started, message))
                continue
            try:
                readings.update(driver.read(tags))
                health.record_success(self.clock.now())
            except DriverError as exc:
                errors.append(f"{device}: {exc}")
                health.record_failure(self.clock.now(), str(exc))
                try:
                    driver.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                readings.update(_bad(tags, started, str(exc)))

        finished = self.clock.now()
        duration = max(0.0, finished - started)
        group.scans += 1
        group.last_duration = duration
        group.max_duration = max(group.max_duration, duration)
        if errors:
            group.failures += 1

        overrun = duration > group.period * self.overrun_ratio
        if overrun:
            group.overruns += 1

        # Schedule the next scan on the original grid so a slow scan does not
        # drift the whole schedule. If the grid is already in the past --
        # meaning the scan took longer than the period -- skip forward whole
        # periods rather than trying to catch up, which would queue scans
        # faster than the device can answer them.
        group.next_due += group.period
        if group.next_due <= finished:
            missed = math.ceil((finished - group.next_due) / group.period)
            group.next_due += missed * group.period

        self.values.update(readings)
        self.scan_count += 1
        result = ScanResult(group.name, started, duration, readings, len(blocks), overrun, errors)
        for callback in self.on_readings:
            callback(result)
        return result

    def poll_once(self) -> list[ScanResult]:
        """Run every group that is currently due."""
        return [self.poll_group(group) for group in self.due_groups()]

    def run(self, duration: float, sleep: bool = True) -> list[ScanResult]:
        """Run the scan loop for ``duration`` seconds of clock time.

        With a :class:`~factorylink.clock.ManualClock` this executes the whole
        run instantly and deterministically, which is how the demo and the
        tests work.
        """
        deadline = self.clock.now() + duration
        results: list[ScanResult] = []
        while self.clock.now() < deadline:
            due = self.due_groups()
            if due:
                results.extend(self.poll_group(g) for g in due)
                continue
            if not sleep:
                break
            wait = min(self.next_due_time(), deadline) - self.clock.now()
            if wait <= 0:
                break
            self.clock.sleep(wait)
        return results

    # -- connection handling ----------------------------------------------

    def _ensure_connected(self, driver: Driver, health: ConnectionHealth, now: float) -> bool:
        if driver.is_connected:
            health.connected = True
            return True
        if not health.may_retry(now):
            return False
        try:
            driver.connect()
        except Exception as exc:  # noqa: BLE001 - a dead device is expected
            health.record_failure(now, str(exc))
            return False
        health.record_success(now)
        return True

    # -- reporting ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Everything the dashboard and CLI need about the loop's health."""
        return {
            "scans": self.scan_count,
            "groups": [g.as_dict() for g in self.groups.values()],
            "devices": [h.as_dict() for h in self.health.values()],
            "blocks": [str(b) for b in self.plan()],
        }

    def read_efficiency(self) -> dict[str, float]:
        """Requests saved by coalescing, per group.

        Returned as ``{group: requests_after / requests_before}``; lower is
        better. This is a measurement of the current tag map, not a claim.
        """
        out: dict[str, float] = {}
        for name, group in self.groups.items():
            before = len(group.tags)
            after = len(self._blocks[name])
            out[name] = (after / before) if before else 1.0
        return out


def _bad(tags: Sequence[TagDef], now: float, error: str) -> dict[str, Reading]:
    return {t.name: Reading(t.name, None, now, Quality.BAD, error=error) for t in tags}
