#!/usr/bin/env python3
"""Poll the simulated bottling line and show what coalescing saves.

Run:  python3 examples/02_poll_the_simulated_line.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from factorylink.clock import ManualClock  # noqa: E402
from factorylink.poller import Poller, coalesce_blocks  # noqa: E402
from factorylink.protocols.simulator import build_simulation  # noqa: E402


def main() -> int:
    """Run a one-minute scan in virtual time and report the read plan."""
    clock = ManualClock(0.0)
    _, driver, db = build_simulation(clock=clock, seed=1234)
    poller = Poller(
        {"line1": driver}, db, {"fast": 0.5, "normal": 2.0, "slow": 10.0}, clock=clock
    )

    print(f"{len(db)} tags across {len(db.devices)} device(s), {len(db.groups)} poll groups")
    print()
    print("READ PLAN (what actually goes on the wire)")
    print("-" * 72)
    for group in db.groups:
        tags = db.by_group(group)
        blocks = coalesce_blocks(tags, max_gap=8)
        period = poller.groups[group].period
        print(f"{group:<8} every {period:>5.1f}s  {len(tags):>3} tags -> {len(blocks)} request(s)")
        for block in blocks:
            print(f"           {block}")
    print("-" * 72)

    poller.run(60.0)
    print()
    print("AFTER 60 SIMULATED SECONDS")
    print("-" * 72)
    for group in poller.groups.values():
        naive = group.scans * len(group.tags)
        actual = group.scans * len(poller.blocks_for(group.name))
        print(
            f"{group.name:<8} {group.scans:>4} scans   "
            f"one request per tag: {naive:>5}   coalesced: {actual:>4}"
        )
    print("-" * 72)
    for name in ("conveyor_speed", "motor_current", "tank_level", "fill_temperature"):
        reading = poller.values[name]
        tag = db[name]
        print(f"{name:<20} {tag.format_value(reading.value):>16}   raw={reading.raw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
