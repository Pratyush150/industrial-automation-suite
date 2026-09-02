"""The driver interface every protocol implementation satisfies.

A driver's whole job is: given a list of :class:`~factorylink.tags.TagDef`,
return a :class:`Reading` per tag with a value, a timestamp and a quality flag.
Everything above it -- polling, alarms, history, OEE, the dashboard -- is
protocol-agnostic, which is why the entire test suite can run against the
built-in simulator and still exercise the real code paths.

Quality matters more than it looks. A control system that cannot distinguish
"the tank is at 0%" from "I did not manage to read the tank" will eventually
raise a low-level alarm because a switch rebooted.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from ..tags import TagDef

__all__ = [
    "Quality",
    "Reading",
    "DriverError",
    "ConnectionError_",
    "ProtocolError",
    "Driver",
    "OptionalDependencyMissing",
    "require",
]


class Quality(str, Enum):
    """OPC-style quality, reduced to the three states that matter."""

    GOOD = "good"
    BAD = "bad"
    STALE = "stale"

    @property
    def usable(self) -> bool:
        """True only for GOOD; alarms and history must ignore the rest."""
        return self is Quality.GOOD


@dataclass(frozen=True)
class Reading:
    """One tag value at one instant."""

    tag: str
    value: float | bool | None
    timestamp: float
    quality: Quality = Quality.GOOD
    raw: tuple[int, ...] | None = None
    error: str | None = None

    @property
    def good(self) -> bool:
        """Convenience alias for ``quality is Quality.GOOD``."""
        return self.quality is Quality.GOOD

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form used by the dashboard API."""
        return {
            "tag": self.tag,
            "value": self.value,
            "timestamp": self.timestamp,
            "quality": self.quality.value,
            "error": self.error,
        }


class DriverError(Exception):
    """Base class for driver failures."""


class ConnectionError_(DriverError):
    """Transport is down: socket refused, serial port gone, device offline."""


class ProtocolError(DriverError):
    """Device answered, but the answer was not usable."""


class OptionalDependencyMissing(DriverError):
    """A driver was constructed without its optional third-party library."""


def require(module: Any, name: str, install_hint: str) -> Any:
    """Return ``module`` or raise a clear error naming the missing package."""
    if module is None:
        raise OptionalDependencyMissing(
            f"{name} needs the '{install_hint}' package, which is not installed. "
            f"Install it with `pip install {install_hint}`, or run against the "
            f"built-in simulator driver, which needs nothing."
        )
    return module


@dataclass
class DriverStats:
    """Counters every driver keeps, surfaced by the dashboard and CLI."""

    reads: int = 0
    read_failures: int = 0
    writes: int = 0
    write_failures: int = 0
    connects: int = 0
    disconnects: int = 0
    last_error: str | None = None
    last_read_duration: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "reads": self.reads,
            "read_failures": self.read_failures,
            "writes": self.writes,
            "write_failures": self.write_failures,
            "connects": self.connects,
            "disconnects": self.disconnects,
            "last_error": self.last_error,
            "last_read_duration": round(self.last_read_duration, 6),
        }


class Driver(ABC):
    """Abstract protocol driver.

    Subclasses implement :meth:`connect`, :meth:`disconnect`, :meth:`read` and
    :meth:`write`. Everything else -- statistics, context-manager support,
    quality defaults -- is provided here so the concrete drivers stay small.
    """

    #: Human-readable protocol name, shown in the CLI and dashboard.
    protocol: str = "abstract"

    def __init__(self, device: str = "plc1") -> None:
        self.device = device
        self._connected = False
        self.stats = DriverStats()

    # -- lifecycle --------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """True when the transport is believed to be up."""
        return self._connected

    @abstractmethod
    def connect(self) -> None:
        """Open the transport. Must be idempotent."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the transport. Must be idempotent and must not raise."""

    def __enter__(self) -> "Driver":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.disconnect()

    # -- data -------------------------------------------------------------

    @abstractmethod
    def read(self, tags: Sequence[TagDef]) -> dict[str, Reading]:
        """Read a batch of tags and return one :class:`Reading` per tag name.

        Implementations must return an entry for every requested tag, using
        ``Quality.BAD`` rather than omitting a tag on failure.
        """

    @abstractmethod
    def write(self, tag: TagDef, value: float | bool) -> None:
        """Write one engineering-unit value to one tag.

        Drivers do not enforce policy. Allow-listing, clamping and rate
        limiting live in :mod:`factorylink.safety`, above this layer.
        """

    # -- helpers for subclasses -------------------------------------------

    def _now(self) -> float:
        return time.time()

    def bad_readings(
        self, tags: Sequence[TagDef], error: str, timestamp: float | None = None
    ) -> dict[str, Reading]:
        """Build a BAD reading for every tag in a failed batch."""
        stamp = self._now() if timestamp is None else timestamp
        return {
            tag.name: Reading(tag.name, None, stamp, Quality.BAD, error=error) for tag in tags
        }

    def describe(self) -> Mapping[str, Any]:
        """Short description used in diagnostics output."""
        return {
            "device": self.device,
            "protocol": self.protocol,
            "connected": self.is_connected,
            "stats": self.stats.as_dict(),
        }


@dataclass
class ModbusEndpoint:
    """Connection parameters shared by the TCP and RTU Modbus drivers."""

    host: str = "127.0.0.1"
    port: int = 502
    unit_id: int = 1
    timeout: float = 1.0
    retries: int = 2
    # Serial-only fields, ignored by the TCP driver.
    serial_port: str = "/dev/ttyUSB0"
    baudrate: int = 9600
    parity: str = "N"
    stopbits: int = 1
    bytesize: int = 8
    extra: dict[str, Any] = field(default_factory=dict)
