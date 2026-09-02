"""Modbus/TCP driver built on pymodbus, with the import guarded.

pymodbus is optional. Import this module without it and everything still
works: the class exists, its helpers are importable and unit-testable, and
constructing a driver raises a clear message telling you what to install.
That is what lets the whole test suite run offline.

What this driver adds over a raw pymodbus call:

* it decodes through :mod:`factorylink.protocols.modbus_codec`, so word and
  byte order come from the tag database rather than from a decoder builder
  buried in the call site;
* it coalesces the requested tags into the fewest legal reads;
* it never raises out of :meth:`read` for a device problem -- it returns BAD
  quality readings, so one dead device does not stop the scan.
"""

from __future__ import annotations

from typing import Any, Sequence

from ..datatypes import MAX_READ_COUNT, RegisterArea, is_bit_area
from ..tags import TagDef
from .base import ConnectionError_, Driver, ModbusEndpoint, ProtocolError, Quality, Reading, require
from .modbus_codec import decode_registers, encode_value

try:  # pragma: no cover - depends on the environment
    from pymodbus.client import ModbusTcpClient as _ModbusTcpClient  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001 - any import failure means "not available"
    _ModbusTcpClient = None  # type: ignore[assignment]

__all__ = ["ModbusTcpDriver", "PYMODBUS_AVAILABLE"]

#: True when pymodbus could be imported.
PYMODBUS_AVAILABLE = _ModbusTcpClient is not None


class ModbusTcpDriver(Driver):
    """Read and write tags over Modbus/TCP."""

    protocol = "modbus-tcp"

    def __init__(self, endpoint: ModbusEndpoint | None = None, device: str = "plc1") -> None:
        super().__init__(device=device)
        require(_ModbusTcpClient, "ModbusTcpDriver", "pymodbus")
        self.endpoint = endpoint or ModbusEndpoint()
        self._client: Any = None

    # -- lifecycle --------------------------------------------------------

    def connect(self) -> None:
        """Open the TCP socket."""
        if self._connected:
            return
        self._client = _ModbusTcpClient(  # type: ignore[misc]
            host=self.endpoint.host,
            port=self.endpoint.port,
            timeout=self.endpoint.timeout,
        )
        if not self._client.connect():
            self._client = None
            raise ConnectionError_(
                f"{self.device}: cannot open {self.endpoint.host}:{self.endpoint.port}"
            )
        self._connected = True
        self.stats.connects += 1

    def disconnect(self) -> None:
        """Close the socket. Safe to call twice."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass
        self._client = None
        if self._connected:
            self.stats.disconnects += 1
        self._connected = False

    # -- reads ------------------------------------------------------------

    def read(self, tags: Sequence[TagDef]) -> dict[str, Reading]:
        """Read every tag, coalescing them into the fewest legal requests."""
        from ..poller import coalesce_blocks  # local import keeps the layering acyclic

        if not self._connected or self._client is None:
            raise ConnectionError_(f"{self.device}: not connected")
        now = self._now()
        out: dict[str, Reading] = {}
        for block in coalesce_blocks(tags):
            try:
                raw = self._read_block(block.area, block.start, block.count)
            except Exception as exc:  # noqa: BLE001 - degraded, not fatal
                self.stats.read_failures += 1
                self.stats.last_error = str(exc)
                out.update(self.bad_readings(block.tags, str(exc), now))
                continue
            self.stats.reads += 1
            for tag in block.tags:
                out[tag.name] = self._decode(tag, block.start, raw, now)
        return out

    def _read_block(self, area: RegisterArea, start: int, count: int) -> list[Any]:
        limit = MAX_READ_COUNT[area]
        if count > limit:
            raise ProtocolError(f"read of {count} exceeds the {limit} limit for {area.value}")
        client = self._client
        unit = self.endpoint.unit_id
        readers = {
            RegisterArea.HOLDING: client.read_holding_registers,
            RegisterArea.INPUT: client.read_input_registers,
            RegisterArea.COIL: client.read_coils,
            RegisterArea.DISCRETE: client.read_discrete_inputs,
        }
        result = _call_pymodbus(readers[area], start, count, unit)
        if result is None or getattr(result, "isError", lambda: False)():
            raise ProtocolError(f"{area.value} read {start}+{count} failed: {result!r}")
        if is_bit_area(area):
            return list(result.bits)[:count]
        return list(result.registers)

    def _decode(
        self, tag: TagDef, block_start: int, raw: Sequence[Any], now: float
    ) -> Reading:
        offset = tag.address - block_start
        chunk = raw[offset : offset + tag.register_count]
        if len(chunk) != tag.register_count:
            return Reading(tag.name, None, now, Quality.BAD, error="short response")
        try:
            if is_bit_area(tag.area):
                return Reading(tag.name, bool(chunk[0]), now, Quality.GOOD)
            decoded = decode_registers(
                [int(c) for c in chunk], tag.data_type, tag.word_order, tag.byte_order, tag.bit
            )
            return Reading(
                tag.name,
                tag.to_engineering(decoded),
                now,
                Quality.GOOD,
                raw=tuple(int(c) for c in chunk),
            )
        except Exception as exc:  # noqa: BLE001
            return Reading(tag.name, None, now, Quality.BAD, error=str(exc))

    # -- writes -----------------------------------------------------------

    def write(self, tag: TagDef, value: float | bool) -> None:
        """Write one engineering value. Policy checks belong upstream."""
        if not self._connected or self._client is None:
            raise ConnectionError_(f"{self.device}: not connected")
        client = self._client
        unit = self.endpoint.unit_id
        self.stats.writes += 1
        try:
            if tag.area is RegisterArea.COIL:
                result = _call_pymodbus(client.write_coil, tag.address, bool(value), unit)
            elif tag.area is RegisterArea.HOLDING:
                if tag.bit is not None:
                    current = self._read_block(RegisterArea.HOLDING, tag.address, 1)[0]
                    regs = encode_value(
                        bool(value),
                        tag.data_type,
                        tag.word_order,
                        tag.byte_order,
                        tag.bit,
                        int(current),
                    )
                else:
                    regs = encode_value(
                        tag.to_raw(value), tag.data_type, tag.word_order, tag.byte_order
                    )
                result = _call_pymodbus(client.write_registers, tag.address, regs, unit)
            else:
                raise ProtocolError(f"{tag.area.value} is read-only on Modbus")
        except Exception as exc:  # noqa: BLE001
            self.stats.write_failures += 1
            self.stats.last_error = str(exc)
            raise
        if result is None or getattr(result, "isError", lambda: False)():
            self.stats.write_failures += 1
            raise ProtocolError(f"write to {tag.name} failed: {result!r}")


def _call_pymodbus(func: Any, address: int, payload: Any, unit: int) -> Any:
    """Call a pymodbus method across the 2.x/3.x keyword rename.

    pymodbus 2.x used ``unit=``, 3.0 used ``slave=``, and 3.7 moved back
    towards ``device_id=``/positional. Rather than pinning a version, try the
    spellings in turn. This is exactly the kind of thing that breaks a
    deployment six months after handover.
    """
    attempts = (
        {"slave": unit},
        {"unit": unit},
        {"device_id": unit},
        {},
    )
    last_error: Exception | None = None
    for kwargs in attempts:
        try:
            if payload is None:
                return func(address, **kwargs)
            return func(address, payload, **kwargs)
        except TypeError as exc:  # wrong keyword for this pymodbus version
            last_error = exc
            continue
    raise ProtocolError(f"no compatible pymodbus call signature found: {last_error}")
