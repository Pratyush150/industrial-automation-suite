#!/usr/bin/env python3
"""Show, with numbers, what a missing deadband costs an operator.

Same signal, same limit, three configurations. Run:

    python3 examples/03_alarm_chatter.py
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from factorylink.alarms import AlarmEngine, AlarmSpec, AlarmType, Severity  # noqa: E402
from factorylink.clock import ManualClock  # noqa: E402

SCAN_PERIOD = 0.5
DURATION = 3600.0
LIMIT = 18.0


def signal():
    """A motor current parked right on its alarm limit, with 0.3 A of noise."""
    rng = random.Random(20260101)
    steps = int(DURATION / SCAN_PERIOD)
    return [(i * SCAN_PERIOD, LIMIT + rng.gauss(0.0, 0.3)) for i in range(steps)]


def run(name: str, **spec_kwargs) -> tuple[str, int]:
    """Drive one configuration over the signal and count raised events."""
    clock = ManualClock(0.0)
    spec = AlarmSpec(
        name="motor_current.hi",
        tag="motor_current",
        alarm_type=AlarmType.HI,
        limit=LIMIT,
        severity=Severity.HIGH,
        **spec_kwargs,
    )
    engine = AlarmEngine([spec], clock=clock)
    raised = 0
    for timestamp, value in signal():
        for event in engine.update({"motor_current": value}, now=timestamp):
            if event.kind == "raised":
                raised += 1
    return name, raised


def main() -> int:
    """Compare three alarm configurations on the same hour of data."""
    print(f"motor current sitting on an {LIMIT} A high limit, 0.3 A RMS noise")
    print(f"{DURATION / 60:.0f} minutes at a {SCAN_PERIOD}s scan "
          f"({int(DURATION / SCAN_PERIOD)} samples)")
    print()
    print(f"{'configuration':<44}{'alarms raised':>14}{'per hour':>10}")
    print("-" * 68)
    rows = [
        run("no deadband, no delay", deadband=0.0, on_delay=0.0),
        run("deadband 1.0 A, no delay", deadband=1.0, on_delay=0.0),
        run("deadband 1.0 A, 5 s on-delay", deadband=1.0, on_delay=5.0),
        run("deadband 1.0 A, 5 s on / 10 s off", deadband=1.0, on_delay=5.0, off_delay=10.0),
    ]
    for name, count in rows:
        print(f"{name:<44}{count:>14}{count / (DURATION / 3600.0):>10.0f}")
    print("-" * 68)
    print()
    print("EEMUA 191 calls more than 10 alarms in 10 minutes a flood, and about")
    print("6 alarms per hour a manageable steady-state load for one operator.")
    print("The first row is one tag. A plant has thousands.")
    print()
    print("This is why an alarm list nobody reads is usually a configuration")
    print("problem, not a discipline problem.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
