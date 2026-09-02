"""Time-series storage in SQLite, with swinging-door compression.

Why compression at all
----------------------
Thirty tags at 2 Hz is 60 rows/second, 5.2 million rows/day, ~180 million
rows/month. Most of those rows say the same thing as the row before them: a
tank level that is not moving does not need 172,800 samples a day to be
faithfully represented. Storing them costs disk, makes every trend query slow,
and buys nothing.

Two levels of reduction are implemented, in order:

1. **Deadband (report by exception).** Drop a sample if it is within a
   deadband of the last *stored* value. Cheap, one comparison, no memory. It
   handles the "signal is parked" case perfectly and the "signal is ramping"
   case badly -- a slow ramp gets quantised into a staircase.

2. **Swinging door.** The industry-standard trend compression. Instead of
   comparing to the last stored value, it asks: can a straight line from the
   last archived point still pass within tolerance of every point since? While
   the answer is yes, nothing is stored. When it becomes no, the *previous*
   point is archived and becomes the new pivot. A linear ramp compresses to two
   points regardless of length, and the reconstruction error stays bounded.

The trade is that a compressed history is an approximation. The tolerance is a
number you choose per tag and it should be tied to the instrument's actual
accuracy: compressing a +/-0.5 degC thermocouple to +/-0.1 degC is storing
noise.

Everything here uses only the standard library.
"""

from __future__ import annotations

import csv
import os
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .clock import Clock, SystemClock
from .protocols.base import Quality, Reading

__all__ = [
    "SwingingDoor",
    "Sample",
    "Bucket",
    "Historian",
]


@dataclass(frozen=True)
class Sample:
    """One archived point."""

    tag: str
    timestamp: float
    value: float
    quality: str = Quality.GOOD.value


@dataclass
class Bucket:
    """Aggregated statistics for one time interval."""

    start: float
    end: float
    count: int
    minimum: float | None
    maximum: float | None
    average: float | None
    first: float | None = None
    last: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "start": self.start,
            "end": self.end,
            "count": self.count,
            "min": self.minimum,
            "max": self.maximum,
            "avg": self.average,
            "first": self.first,
            "last": self.last,
        }


class SwingingDoor:
    """Swinging-door trend compression for one signal.

    The algorithm keeps two slope bounds from the last archived point:

    * ``upper`` = the smallest slope that still passes *above* every seen
      point minus tolerance;
    * ``lower`` = the largest slope that still passes *below* every seen point
      plus tolerance.

    While ``upper >= lower`` a straight line from the pivot within tolerance of
    every point exists, so nothing needs archiving. When a new point makes the
    doors cross, the previous point is archived as the new pivot.

    ``max_interval`` forces a heartbeat point even when nothing changes, so a
    flat signal still proves it was being read. ``min_interval`` throttles the
    archive rate for very noisy signals.
    """

    def __init__(
        self,
        tolerance: float,
        max_interval: float | None = None,
        min_interval: float = 0.0,
    ) -> None:
        if tolerance < 0:
            raise ValueError("tolerance cannot be negative")
        if min_interval < 0:
            raise ValueError("min_interval cannot be negative")
        self.tolerance = float(tolerance)
        self.max_interval = max_interval
        self.min_interval = float(min_interval)

        self.pivot: tuple[float, float] | None = None
        self.held: tuple[float, float] | None = None
        self.upper = float("inf")
        self.lower = float("-inf")
        self.seen = 0
        self.archived = 0

    @property
    def compression_ratio(self) -> float:
        """Archived points divided by points seen. Lower is more compression."""
        return self.archived / self.seen if self.seen else 1.0

    def _reset_doors(self, pivot: tuple[float, float]) -> None:
        self.pivot = pivot
        self.held = None
        self.upper = float("inf")
        self.lower = float("-inf")

    def update(self, timestamp: float, value: float) -> list[tuple[float, float]]:
        """Feed one sample; return the points that should be archived now."""
        t = float(timestamp)
        v = float(value)
        self.seen += 1

        if self.pivot is None:
            self._reset_doors((t, v))
            self.archived += 1
            return [(t, v)]

        t0, v0 = self.pivot
        dt = t - t0
        if dt <= 0:
            # Out-of-order or duplicate timestamp: ignore rather than corrupt
            # the door slopes. A historian that trusts unsorted timestamps
            # produces trends that go backwards.
            self.seen -= 1
            return []

        if self.max_interval is not None and dt >= self.max_interval:
            out = self._archive_current(t, v)
            return out

        upper = (v + self.tolerance - v0) / dt
        lower = (v - self.tolerance - v0) / dt
        new_upper = min(self.upper, upper)
        new_lower = max(self.lower, lower)

        if new_upper < new_lower:
            # The doors closed: no straight line from the pivot covers every
            # point within tolerance. Archive the last held point.
            return self._archive_current(t, v)

        self.upper = new_upper
        self.lower = new_lower
        self.held = (t, v)
        return []

    def _archive_current(self, t: float, v: float) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        candidate = self.held if self.held is not None else (t, v)
        if self.min_interval and self.pivot is not None:
            if candidate[0] - self.pivot[0] < self.min_interval:
                candidate = (t, v)
        out.append(candidate)
        self.archived += 1
        self._reset_doors(candidate)
        if candidate != (t, v):
            # Re-run the current point against the fresh doors so it is not lost.
            self.seen -= 1
            out.extend(self.update(t, v))
        return out

    def flush(self) -> list[tuple[float, float]]:
        """Archive the pending point at the end of a run."""
        if self.held is None:
            return []
        point = self.held
        self.archived += 1
        self._reset_doors(point)
        return [point]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    tag       TEXT    NOT NULL,
    ts        REAL    NOT NULL,
    value     REAL,
    quality   TEXT    NOT NULL DEFAULT 'good',
    PRIMARY KEY (tag, ts)
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples (ts);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL    NOT NULL,
    source    TEXT    NOT NULL,
    kind      TEXT    NOT NULL,
    severity  INTEGER NOT NULL DEFAULT 0,
    message   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
"""


@dataclass
class _TagPolicy:
    """Per-tag compression settings."""

    deadband: float = 0.0
    tolerance: float = 0.0
    max_interval: float | None = 300.0
    door: SwingingDoor | None = None
    last_stored: float | None = None
    last_stored_ts: float | None = None
    received: int = 0
    stored: int = 0


class Historian:
    """SQLite-backed time-series store with per-tag compression.

    Use it as a context manager, or call :meth:`close` when finished.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] = ":memory:",
        clock: Clock | None = None,
        default_deadband: float = 0.0,
        default_tolerance: float = 0.0,
        default_max_interval: float | None = 300.0,
        compression: bool = True,
    ) -> None:
        self.path = str(path)
        self.clock = clock or SystemClock()
        self.compression = compression
        self.default_deadband = float(default_deadband)
        self.default_tolerance = float(default_tolerance)
        self.default_max_interval = default_max_interval
        # check_same_thread=False plus an explicit lock: the dashboard serves
        # requests from worker threads while the scan loop writes, and SQLite
        # connections are otherwise bound to their creating thread.
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(_SCHEMA)
            self.conn.commit()
        self.policies: dict[str, _TagPolicy] = {}

    def __enter__(self) -> "Historian":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Flush pending compressed points and close the database."""
        try:
            self.flush()
        finally:
            with self.lock:
                self.conn.commit()
                self.conn.close()

    # -- configuration -----------------------------------------------------

    def configure_tag(
        self,
        tag: str,
        deadband: float | None = None,
        tolerance: float | None = None,
        max_interval: float | None = -1.0,
    ) -> None:
        """Set the compression policy for one tag."""
        policy = self.policies.setdefault(tag, self._default_policy())
        if deadband is not None:
            policy.deadband = float(deadband)
        if tolerance is not None:
            policy.tolerance = float(tolerance)
        if max_interval != -1.0:
            policy.max_interval = max_interval
        policy.door = (
            SwingingDoor(policy.tolerance, policy.max_interval)
            if self.compression and policy.tolerance > 0
            else None
        )

    def configure_from_tags(self, tags: Iterable[Any], tolerance_factor: float = 1.0) -> None:
        """Take deadbands from a tag database and derive door tolerances.

        The default tolerance is the tag's configured deadband: if a change
        smaller than the deadband is not worth reporting live, it is not worth
        archiving either.
        """
        for tag in tags:
            band = float(getattr(tag, "deadband", 0.0) or 0.0)
            self.configure_tag(
                tag.name,
                deadband=band,
                tolerance=band * tolerance_factor,
                max_interval=self.default_max_interval,
            )

    def _default_policy(self) -> _TagPolicy:
        policy = _TagPolicy(
            deadband=self.default_deadband,
            tolerance=self.default_tolerance,
            max_interval=self.default_max_interval,
        )
        if self.compression and policy.tolerance > 0:
            policy.door = SwingingDoor(policy.tolerance, policy.max_interval)
        return policy

    # -- writing -----------------------------------------------------------

    def record(
        self,
        tag: str,
        value: float | bool | None,
        timestamp: float | None = None,
        quality: Quality | str = Quality.GOOD,
    ) -> int:
        """Store one sample, applying deadband then swinging door.

        Returns the number of rows actually written (0, 1 or 2).
        """
        if value is None:
            return 0
        quality_text = quality.value if isinstance(quality, Quality) else str(quality)
        if quality_text != Quality.GOOD.value:
            return 0
        stamp = self.clock.now() if timestamp is None else float(timestamp)
        numeric = float(value)
        policy = self.policies.setdefault(tag, self._default_policy())
        policy.received += 1

        if not self.compression:
            return self._insert(tag, stamp, numeric, quality_text, policy)

        if policy.deadband > 0 and policy.last_stored is not None:
            aged_out = (
                policy.max_interval is not None
                and policy.last_stored_ts is not None
                and stamp - policy.last_stored_ts >= policy.max_interval
            )
            if abs(numeric - policy.last_stored) <= policy.deadband and not aged_out:
                return 0

        if policy.door is None:
            return self._insert(tag, stamp, numeric, quality_text, policy)

        written = 0
        for point_t, point_v in policy.door.update(stamp, numeric):
            written += self._insert(tag, point_t, point_v, quality_text, policy)
        return written

    def record_reading(self, reading: Reading) -> int:
        """Store one :class:`~factorylink.protocols.base.Reading`."""
        value = reading.value
        if isinstance(value, bool):
            value = float(value)
        return self.record(reading.tag, value, reading.timestamp, reading.quality)

    def record_readings(self, readings: Mapping[str, Reading]) -> int:
        """Store a whole scan's worth of readings; returns rows written."""
        return sum(self.record_reading(r) for r in readings.values())

    def _insert(
        self, tag: str, stamp: float, value: float, quality: str, policy: _TagPolicy
    ) -> int:
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO samples (tag, ts, value, quality) VALUES (?, ?, ?, ?)",
                (tag, stamp, value, quality),
            )
        policy.last_stored = value
        policy.last_stored_ts = stamp
        policy.stored += 1
        return 1

    def flush(self) -> int:
        """Archive every pending swinging-door point and commit."""
        written = 0
        for tag, policy in self.policies.items():
            if policy.door is None:
                continue
            for point_t, point_v in policy.door.flush():
                written += self._insert(tag, point_t, point_v, Quality.GOOD.value, policy)
        with self.lock:
            self.conn.commit()
        return written

    def record_event(
        self,
        source: str,
        kind: str,
        message: str,
        severity: int = 0,
        timestamp: float | None = None,
    ) -> None:
        """Append one event (alarm transition, stop, operator action)."""
        stamp = self.clock.now() if timestamp is None else float(timestamp)
        with self.lock:
            self.conn.execute(
                "INSERT INTO events (ts, source, kind, severity, message) VALUES (?, ?, ?, ?, ?)",
                (stamp, source, kind, int(severity), message),
            )

    # -- reading -----------------------------------------------------------

    def tags(self) -> list[str]:
        """Every tag that has at least one archived sample."""
        with self.lock:
            rows = self.conn.execute("SELECT DISTINCT tag FROM samples ORDER BY tag").fetchall()
        return [row["tag"] for row in rows]

    def count(self, tag: str | None = None) -> int:
        """Number of archived samples, optionally for one tag."""
        with self.lock:
            if tag is None:
                row = self.conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()
            else:
                row = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM samples WHERE tag = ?", (tag,)
                ).fetchone()
        return int(row["n"])

    def query(
        self,
        tag: str,
        start: float | None = None,
        end: float | None = None,
        limit: int | None = None,
    ) -> list[Sample]:
        """Archived samples for one tag in a time window, oldest first."""
        sql = "SELECT tag, ts, value, quality FROM samples WHERE tag = ?"
        params: list[Any] = [tag]
        if start is not None:
            sql += " AND ts >= ?"
            params.append(float(start))
        if end is not None:
            sql += " AND ts <= ?"
            params.append(float(end))
        sql += " ORDER BY ts"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [Sample(r["tag"], r["ts"], r["value"], r["quality"]) for r in rows]

    def latest(self, tag: str) -> Sample | None:
        """Most recent archived sample for one tag."""
        with self.lock:
            row = self.conn.execute(
                "SELECT tag, ts, value, quality FROM samples WHERE tag = ? "
                "ORDER BY ts DESC LIMIT 1",
                (tag,),
            ).fetchone()
        if row is None:
            return None
        return Sample(row["tag"], row["ts"], row["value"], row["quality"])

    def interpolate(self, tag: str, timestamp: float) -> float | None:
        """Reconstruct a value at ``timestamp`` by linear interpolation.

        This is how a compressed history is read back: the archived points are
        the vertices of a piecewise-linear signal.
        """
        with self.lock:
            before = self.conn.execute(
                "SELECT ts, value FROM samples WHERE tag = ? AND ts <= ? "
                "ORDER BY ts DESC LIMIT 1",
                (tag, float(timestamp)),
            ).fetchone()
            after = self.conn.execute(
                "SELECT ts, value FROM samples WHERE tag = ? AND ts >= ? "
                "ORDER BY ts ASC LIMIT 1",
                (tag, float(timestamp)),
            ).fetchone()
        if before is None and after is None:
            return None
        if before is None:
            return float(after["value"])
        if after is None:
            return float(before["value"])
        if after["ts"] == before["ts"]:
            return float(before["value"])
        span = after["ts"] - before["ts"]
        weight = (float(timestamp) - before["ts"]) / span
        return float(before["value"]) + weight * (float(after["value"]) - float(before["value"]))

    def aggregate(
        self, tag: str, start: float, end: float, interval: float
    ) -> list[Bucket]:
        """Min/max/avg/count per fixed interval, computed in SQL."""
        if interval <= 0:
            raise ValueError("interval must be positive")
        out: list[Bucket] = []
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT CAST((ts - ?) / ? AS INTEGER) AS bucket,
                       COUNT(*) AS n,
                       MIN(value) AS vmin,
                       MAX(value) AS vmax,
                       AVG(value) AS vavg
                FROM samples
                WHERE tag = ? AND ts >= ? AND ts < ?
                GROUP BY bucket
                ORDER BY bucket
                """,
                (float(start), float(interval), tag, float(start), float(end)),
            ).fetchall()
            for row in rows:
                bucket_start = float(start) + int(row["bucket"]) * float(interval)
                bucket_end = bucket_start + float(interval)
                edges = self.conn.execute(
                    """
                    SELECT
                      (SELECT value FROM samples WHERE tag = ? AND ts >= ? AND ts < ?
                         ORDER BY ts ASC LIMIT 1) AS vfirst,
                      (SELECT value FROM samples WHERE tag = ? AND ts >= ? AND ts < ?
                         ORDER BY ts DESC LIMIT 1) AS vlast
                    """,
                    (tag, bucket_start, bucket_end, tag, bucket_start, bucket_end),
                ).fetchone()
                out.append(
                    Bucket(
                        start=bucket_start,
                        end=bucket_end,
                        count=int(row["n"]),
                        minimum=row["vmin"],
                        maximum=row["vmax"],
                        average=row["vavg"],
                        first=edges["vfirst"],
                        last=edges["vlast"],
                    )
                )
        return out

    def events(self, start: float | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Recent events, newest first."""
        sql = "SELECT ts, source, kind, severity, message FROM events"
        params: list[Any] = []
        if start is not None:
            sql += " WHERE ts >= ?"
            params.append(float(start))
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # -- housekeeping ------------------------------------------------------

    def apply_retention(self, max_age: float, now: float | None = None) -> int:
        """Delete samples and events older than ``max_age`` seconds.

        Returns the number of sample rows removed. Retention on a plant
        historian is not optional: an unbounded SQLite file eventually fills
        the disk on the box that is also running the HMI.
        """
        stamp = self.clock.now() if now is None else float(now)
        cutoff = stamp - float(max_age)
        with self.lock:
            cursor = self.conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            removed = cursor.rowcount or 0
            self.conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            self.conn.commit()
        return removed

    def vacuum(self) -> None:
        """Reclaim disk space after a retention pass."""
        with self.lock:
            self.conn.commit()
            self.conn.execute("VACUUM")

    def export_csv(
        self,
        path: str | os.PathLike[str],
        tags: Sequence[str] | None = None,
        start: float | None = None,
        end: float | None = None,
    ) -> int:
        """Write archived samples to CSV. Returns the row count written."""
        names = list(tags) if tags else self.tags()
        written = 0
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(["tag", "timestamp", "value", "quality"])
            for name in names:
                for sample in self.query(name, start, end):
                    writer.writerow([sample.tag, f"{sample.timestamp:.3f}", sample.value, sample.quality])
                    written += 1
        return written

    def stats(self) -> dict[str, Any]:
        """Per-tag received/stored counts and the achieved compression."""
        out: dict[str, Any] = {"rows": self.count(), "tags": {}}
        for tag, policy in self.policies.items():
            if not policy.received:
                continue
            out["tags"][tag] = {
                "received": policy.received,
                "stored": policy.stored,
                "ratio": round(policy.stored / policy.received, 4),
                "deadband": policy.deadband,
                "tolerance": policy.tolerance,
            }
        received = sum(p.received for p in self.policies.values())
        stored = sum(p.stored for p in self.policies.values())
        out["received"] = received
        out["stored"] = stored
        out["ratio"] = round(stored / received, 4) if received else 1.0
        return out

    def __iter__(self) -> Iterator[str]:
        return iter(self.tags())
