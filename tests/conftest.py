"""Shared fixtures. Everything is offline and driven by a manual clock."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from factorylink.clock import ManualClock  # noqa: E402
from factorylink.protocols.base import ConnectionError_, Driver, Quality, Reading  # noqa: E402
from factorylink.protocols.simulator import (  # noqa: E402
    ProcessConfig,
    SimulatedPLC,
    SimulatorDriver,
    bottling_line_tags,
)
from factorylink.tags import TagDatabase, TagDef  # noqa: E402


@pytest.fixture()
def clock() -> ManualClock:
    """A clock that starts at zero and only moves when a test moves it."""
    return ManualClock(0.0)


@pytest.fixture()
def db() -> TagDatabase:
    """The built-in bottling-line tag database."""
    return bottling_line_tags()


@pytest.fixture()
def plc() -> SimulatedPLC:
    """A fresh, deterministic simulated PLC."""
    return SimulatedPLC(config=ProcessConfig(), seed=4242)


@pytest.fixture()
def driver(plc: SimulatedPLC, db: TagDatabase, clock: ManualClock) -> SimulatorDriver:
    """A connected simulator driver over the bottling line."""
    drv = SimulatorDriver(plc, db, clock=clock)
    drv.connect()
    plc.sync_image(db)
    return drv


@pytest.fixture()
def repo_root() -> Path:
    """Path to the repository root, for config and CLI tests."""
    return REPO_ROOT


class FlakyDriver(Driver):
    """A driver that fails on demand, for connection-health tests."""

    protocol = "flaky"

    def __init__(self, device: str = "flaky1", fail_connect: bool = True) -> None:
        super().__init__(device=device)
        self.fail_connect = fail_connect
        self.fail_read = False
        self.connect_attempts = 0
        self.read_attempts = 0

    def connect(self) -> None:
        """Open the fake transport, or raise if ``fail_connect`` is set."""
        self.connect_attempts += 1
        if self.fail_connect:
            raise ConnectionError_(f"{self.device}: simulated connect failure")
        self._connected = True
        self.stats.connects += 1

    def disconnect(self) -> None:
        """Close the fake transport."""
        self._connected = False

    def read(self, tags: Sequence[TagDef]) -> dict[str, Reading]:
        """Return zeroes, or raise if ``fail_read`` is set."""
        self.read_attempts += 1
        if self.fail_read:
            raise ConnectionError_(f"{self.device}: simulated read failure")
        return {t.name: Reading(t.name, 0.0, 0.0, Quality.GOOD) for t in tags}

    def write(self, tag: TagDef, value: float | bool) -> None:
        """Accept and discard the write."""


@pytest.fixture()
def flaky_driver() -> FlakyDriver:
    """A driver whose connect attempts fail."""
    return FlakyDriver()
