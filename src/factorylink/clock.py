"""Injectable clocks.

Every timing decision in this package -- poll scheduling, alarm on/off delays,
historian timestamps, OEE windows, rate limits -- goes through a clock object.
The production clock reads the system time; the test clock is advanced by hand.

That is the difference between a test suite that proves an off-delay timer
works and a test suite that sleeps for two seconds and hopes.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "SystemClock", "ManualClock"]


@runtime_checkable
class Clock(Protocol):
    """Minimal clock interface: a monotonic-ish seconds reading and a sleep."""

    def now(self) -> float:
        """Current time in seconds."""

    def sleep(self, seconds: float) -> None:
        """Block (or virtually advance) for ``seconds``."""


class SystemClock:
    """Wall-clock time, used at runtime."""

    __slots__ = ()

    def now(self) -> float:
        """Seconds since the epoch."""
        return time.time()

    def sleep(self, seconds: float) -> None:
        """Sleep for ``seconds`` (negative values are treated as zero)."""
        if seconds > 0:
            time.sleep(seconds)


class ManualClock:
    """A clock that only moves when you move it.

    Used by the whole test suite and by ``--demo``, so a sixty-second
    simulated run completes in milliseconds and produces the same numbers
    every time.
    """

    __slots__ = ("_now", "sleep_calls")

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        self.sleep_calls: list[float] = []

    def now(self) -> float:
        """Current virtual time."""
        return self._now

    def sleep(self, seconds: float) -> None:
        """Advance virtual time instead of blocking."""
        if seconds > 0:
            self.sleep_calls.append(float(seconds))
            self._now += float(seconds)

    def advance(self, seconds: float) -> float:
        """Move the clock forward and return the new time."""
        if seconds < 0:
            raise ValueError("cannot move a clock backwards")
        self._now += float(seconds)
        return self._now

    def set(self, value: float) -> None:
        """Jump the clock to an absolute time."""
        if value < self._now:
            raise ValueError("cannot move a clock backwards")
        self._now = float(value)
