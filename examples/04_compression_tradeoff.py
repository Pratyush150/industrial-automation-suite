#!/usr/bin/env python3
"""Measure what swinging-door compression costs and what it saves.

Run:  python3 examples/04_compression_tradeoff.py
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from factorylink.historian import SwingingDoor  # noqa: E402

SAMPLES = 7200  # one hour at 2 Hz


def signal(kind: str):
    """Three signals with very different compressibility."""
    rng = random.Random(4242)
    if kind == "ramping level":
        return [(i * 0.5, 90.0 - i * 0.008) for i in range(SAMPLES)]
    if kind == "temperature":
        return [
            (i * 0.5, 4.0 + 1.2 * math.sin(i / 900.0) + rng.gauss(0.0, 0.05))
            for i in range(SAMPLES)
        ]
    return [
        (i * 0.5, 12.4 + rng.gauss(0.0, 0.35) + (3.0 if 3000 < i < 3400 else 0.0))
        for i in range(SAMPLES)
    ]


def reconstruct(points, timestamp):
    """Linear interpolation between archived points, as a trend viewer does."""
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t0 <= timestamp <= t1:
            return v0 if t1 == t0 else v0 + (v1 - v0) * (timestamp - t0) / (t1 - t0)
    return points[-1][1]


def main() -> int:
    """Compress three signals at three tolerances and report the trade."""
    print(f"{SAMPLES} samples per signal (one hour at 2 Hz)")
    print()
    print(f"{'signal':<18}{'tolerance':>10}{'stored':>9}{'ratio':>9}"
          f"{'max error':>12}{'bytes/day':>12}")
    print("-" * 70)
    for kind in ("ramping level", "temperature", "noisy current"):
        original = signal(kind)
        for tolerance in (0.05, 0.2, 1.0):
            door = SwingingDoor(tolerance=tolerance, max_interval=600.0)
            archived = []
            for timestamp, value in original:
                archived += door.update(timestamp, value)
            archived += door.flush()
            error = max(abs(v - reconstruct(archived, t)) for t, v in original)
            ratio = len(archived) / len(original)
            # 24 bytes per stored row is a reasonable SQLite estimate for
            # (tag id, float timestamp, float value).
            bytes_per_day = len(archived) * 24 * 24
            print(
                f"{kind:<18}{tolerance:>10.2f}{len(archived):>9}{ratio:>9.3f}"
                f"{error:>12.4f}{bytes_per_day:>12,}"
            )
        print()
    print("Read it this way:")
    print("  * A ramp compresses to almost nothing at any tolerance, because a")
    print("    straight line is exactly what the algorithm stores.")
    print("  * Noise does not compress. If a signal will not compress, the")
    print("    tolerance is smaller than the instrument's real accuracy and you")
    print("    are paying disk to archive noise.")
    print("  * Reconstruction error stays inside twice the tolerance. Choose the")
    print("    tolerance from the instrument spec, not from a storage target.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
