"""Protocol drivers.

Nothing is imported eagerly here. Two reasons:

* :mod:`factorylink.tags` needs the pure scaling helpers from
  :mod:`factorylink.protocols.modbus_codec`, while
  :mod:`factorylink.protocols.base` needs ``TagDef`` from
  :mod:`factorylink.tags`. Keeping this package's ``__init__`` empty of
  imports breaks that cycle at the only place it can be broken cleanly.
* A missing optional package must never be able to break
  ``import factorylink``.

The names below are resolved on first access through :func:`__getattr__`, so
``from factorylink.protocols import Driver`` still works.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ConnectionError_",
    "Driver",
    "DriverError",
    "ModbusEndpoint",
    "OptionalDependencyMissing",
    "ProtocolError",
    "Quality",
    "Reading",
    "available_drivers",
]

_LAZY = {
    "ConnectionError_": "base",
    "Driver": "base",
    "DriverError": "base",
    "ModbusEndpoint": "base",
    "OptionalDependencyMissing": "base",
    "ProtocolError": "base",
    "Quality": "base",
    "Reading": "base",
}


def __getattr__(name: str) -> Any:
    """Resolve the driver base classes on first use."""
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f".{module_name}", __name__), name)
    globals()[name] = value
    return value


def available_drivers() -> dict[str, bool]:
    """Report which drivers can actually be constructed in this environment.

    Useful in a bug report: it answers "is pymodbus installed?" without a
    round trip.
    """
    from . import modbus_rtu, modbus_tcp, mqtt_sparkplug, opcua

    return {
        "simulator": True,
        "modbus_tcp": modbus_tcp.PYMODBUS_AVAILABLE,
        "modbus_rtu": modbus_rtu.PYMODBUS_SERIAL_AVAILABLE,
        "opcua": opcua.ASYNCUA_AVAILABLE,
        "mqtt_sparkplug": mqtt_sparkplug.MQTT_AVAILABLE,
    }
