"""Modbus RTU (serial) driver, with the pymodbus import guarded.

RTU is Modbus over a two- or four-wire serial line, and it fails in ways TCP
does not. The two that cost the most time:

*Inter-frame gap.* RTU has no start or end delimiter. A frame ends when the
line has been silent for 3.5 character times. At 9600 baud with 8N1 that is
about 4 ms; at 115200 baud it is about 0.3 ms, which is short enough that a
USB-serial adapter's latency timer -- typically 16 ms by default -- smears
frames together and you get CRC errors that look like electrical noise. Lower
the FTDI latency timer to 1 ms before you start replacing cable.

*Turnaround delay.* Some slaves need a few milliseconds after the last byte of
the request before they will listen. Hammering them with back-to-back requests
produces intermittent timeouts that move around when you change the poll
order, which is a very effective way to convince yourself the wiring is bad.

Both are configurable here. See docs/FIELD_NOTES.md.
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from ..datatypes import RegisterArea
from ..tags import TagDef
from .base import ConnectionError_, Driver, ModbusEndpoint, Reading, require
from .modbus_tcp import ModbusTcpDriver

try:  # pragma: no cover - depends on the environment
    from pymodbus.client import ModbusSerialClient as _ModbusSerialClient  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001
    _ModbusSerialClient = None  # type: ignore[assignment]

__all__ = ["ModbusRtuDriver", "PYMODBUS_SERIAL_AVAILABLE", "frame_gap_seconds", "char_time_seconds"]

PYMODBUS_SERIAL_AVAILABLE = _ModbusSerialClient is not None


def char_time_seconds(baudrate: int, bits_per_char: int = 11) -> float:
    """Duration of one serial character.

    8N1 is 11 bits on the wire: 1 start + 8 data + 0 parity + 1 stop, plus the
    idle bit the standard counts. 8E1 and 8O1 are also 11 bits.
    """
    if baudrate <= 0:
        raise ValueError("baudrate must be positive")
    return bits_per_char / float(baudrate)


def frame_gap_seconds(baudrate: int) -> float:
    """Minimum silent interval that ends an RTU frame (3.5 character times).

    The Modbus specification fixes this at 1.75 ms for baud rates above
    19200, because chasing sub-millisecond gaps is not worth it.
    """
    if baudrate > 19200:
        return 0.00175
    return 3.5 * char_time_seconds(baudrate)


class ModbusRtuDriver(ModbusTcpDriver):
    """Modbus RTU over a serial port.

    Shares the read/write/decode logic with the TCP driver -- the PDU is
    identical, only the transport differs -- and adds serial-specific timing.
    """

    protocol = "modbus-rtu"

    def __init__(
        self,
        endpoint: ModbusEndpoint | None = None,
        device: str = "plc1",
        turnaround_delay: float | None = None,
    ) -> None:
        # Skip ModbusTcpDriver.__init__ on purpose: it would demand the TCP
        # client. Initialise the abstract base directly, then set up serial.
        Driver.__init__(self, device=device)
        require(_ModbusSerialClient, "ModbusRtuDriver", "pymodbus[serial]")
        self.endpoint = endpoint or ModbusEndpoint()
        self._client: Any = None
        self.turnaround_delay = (
            frame_gap_seconds(self.endpoint.baudrate)
            if turnaround_delay is None
            else float(turnaround_delay)
        )

    def connect(self) -> None:
        """Open the serial port."""
        if self._connected:
            return
        self._client = _ModbusSerialClient(  # type: ignore[misc]
            port=self.endpoint.serial_port,
            baudrate=self.endpoint.baudrate,
            parity=self.endpoint.parity,
            stopbits=self.endpoint.stopbits,
            bytesize=self.endpoint.bytesize,
            timeout=self.endpoint.timeout,
        )
        if not self._client.connect():
            self._client = None
            raise ConnectionError_(
                f"{self.device}: cannot open {self.endpoint.serial_port} "
                f"at {self.endpoint.baudrate} baud"
            )
        self._connected = True
        self.stats.connects += 1

    def read(self, tags: Sequence[TagDef]) -> dict[str, Reading]:
        """Read tags, honouring the inter-frame turnaround delay."""
        result = super().read(tags)
        if self.turnaround_delay > 0:
            time.sleep(self.turnaround_delay)
        return result

    def write(self, tag: TagDef, value: float | bool) -> None:
        """Write one tag, honouring the inter-frame turnaround delay."""
        if tag.area not in (RegisterArea.HOLDING, RegisterArea.COIL):
            raise ConnectionError_(f"{tag.area.value} is not writable over Modbus")
        super().write(tag, value)
        if self.turnaround_delay > 0:
            time.sleep(self.turnaround_delay)
