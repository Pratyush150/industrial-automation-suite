#!/usr/bin/env python3
"""Run a simulated eight-hour shift and print the report a plant manager reads.

Run:  python3 examples/05_shift_report.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from factorylink.clock import ManualClock  # noqa: E402
from factorylink.protocols.simulator import Fault  # noqa: E402
from factorylink.runtime import build_simulated_runtime  # noqa: E402

SHIFT = 8 * 3600.0

#: A plausible shift: a changeover, two jams, a chiller problem and a leak.
SCHEDULE = [
    (1_200.0, Fault.JAM, 240.0),
    (5_400.0, Fault.AIR_LEAK, 600.0),
    (9_000.0, Fault.CHILLER_FAILURE, 1_500.0),
    (16_200.0, Fault.JAM, 420.0),
    (22_000.0, Fault.MOTOR_OVERLOAD, 900.0),
]


def main() -> int:
    """Run the shift and print OEE, the Pareto and the alarm summary."""
    clock = ManualClock(0.0)
    runtime, plc = build_simulated_runtime(clock=clock, seed=20260101)
    for at, fault, duration in SCHEDULE:
        plc.schedule_fault(at, fault, duration)

    runtime.run(SHIFT)
    runtime.historian.flush()

    result = runtime.oee.result(runtime.clock.now())
    print(result.format_report())
    print()
    print(runtime.oee.tracker.format_pareto())
    print()

    summary = runtime.alarms.summary()
    print("=" * 66)
    print("ALARM SUMMARY")
    print("=" * 66)
    print(f"configured {summary['configured']}, active {summary['active']}, "
          f"unacknowledged {summary['unacked']}")
    print(f"alarms raised in the last 10 minutes: {summary['rate_10min']}")
    top = sorted(
        (i for i in runtime.alarms.alarms.values() if i.activations),
        key=lambda i: -i.activations,
    )[:6]
    print(f"{'alarm':<32}{'activations':>12}{'severity':>10}")
    print("-" * 66)
    for instance in top:
        print(f"{instance.spec.name:<32}{instance.activations:>12}"
              f"{instance.spec.severity.name:>10}")
    print("=" * 66)
    print()

    stats = runtime.historian.stats()
    print("=" * 66)
    print("HISTORIAN")
    print("=" * 66)
    print(f"{stats['received']:,} samples received, {stats['stored']:,} archived "
          f"({stats['ratio'] * 100:.1f}%)")
    print(f"uncompressed, this shift would have been "
          f"{stats['received'] * 24 / 1024:,.0f} KiB of rows")
    print("=" * 66)
    runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
