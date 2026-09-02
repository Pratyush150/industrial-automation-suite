"""factorylink -- get data off a PLC and make it visible and actionable.

A small, dependency-light industrial acquisition stack:

* :mod:`factorylink.protocols` -- a driver interface plus Modbus TCP/RTU,
  OPC UA and MQTT Sparkplug B implementations (all optional dependencies,
  guarded) and a fully working simulated PLC that needs nothing at all.
* :mod:`factorylink.protocols.modbus_codec` -- register decoding written from
  first principles, so word order and byte order are configuration rather than
  a guess.
* :mod:`factorylink.tags` -- a validated tag database loaded from YAML or CSV.
* :mod:`factorylink.poller` -- poll groups, register-range coalescing,
  staggering, connection health and slow-scan detection.
* :mod:`factorylink.alarms` -- limits, rate of change, deviation and digital
  alarms with hysteresis, delays, latching, shelving and flood detection.
* :mod:`factorylink.historian` -- SQLite time series with deadband and
  swinging-door compression, aggregation, retention and CSV export.
* :mod:`factorylink.oee` -- availability, performance, quality and the
  downtime Pareto behind them.
* :mod:`factorylink.dashboard` -- a stdlib HTTP server and a single-file page
  with server-rendered SVG trends. No build step, no CDN.
* :mod:`factorylink.safety` -- write-path protection. Read
  :data:`factorylink.safety.SAFETY_NOTICE` before enabling writes.

Quick start, no hardware required::

    from factorylink.clock import ManualClock
    from factorylink.runtime import build_simulated_runtime, format_scan_table

    runtime, plc = build_simulated_runtime(clock=ManualClock())
    runtime.run(300.0)
    print(format_scan_table(runtime))
"""

from __future__ import annotations

__version__ = "0.1.0"
__author__ = "Pratyush Vatsa"
__license__ = "MIT"

from .clock import ManualClock, SystemClock
from .datatypes import ByteOrder, DataType, RegisterArea, WordOrder
from .tags import TagDatabase, TagDef, TagValidationError

__all__ = [
    "__version__",
    "__author__",
    "__license__",
    "ByteOrder",
    "DataType",
    "ManualClock",
    "RegisterArea",
    "SystemClock",
    "TagDatabase",
    "TagDef",
    "TagValidationError",
    "WordOrder",
]
