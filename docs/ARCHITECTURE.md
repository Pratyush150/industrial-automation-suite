# Architecture

## The shape of the thing

```
                      config/tags_bottling_line.yaml
                      config/alarms.yaml
                      config/devices.yaml
                                 |
                                 v
  +---------------------------------------------------------------+
  |  tags.py        TagDatabase: name, device, area, address,      |
  |                 data type, word/byte order, scale, deadband,   |
  |                 poll group, limits. Validated as a whole.      |
  +---------------------------------------------------------------+
                                 |
                                 v
  +---------------------------------------------------------------+
  |  poller.py      poll groups -> coalesce_blocks() -> the        |
  |                 fewest legal reads; staggering; per-device     |
  |                 connection health with backoff; overrun        |
  |                 detection                                      |
  +---------------------------------------------------------------+
                                 |
                                 v
  +---------------------------------------------------------------+
  |  protocols/     Driver ABC: read(tags) -> {name: Reading}      |
  |                                                                 |
  |    simulator.py     bottling-line process model + register      |
  |                     image + driver          (no dependencies)   |
  |    modbus_tcp.py    pymodbus                (guarded import)    |
  |    modbus_rtu.py    pymodbus + serial timing(guarded import)    |
  |    opcua.py         asyncua                 (guarded import)    |
  |    mqtt_sparkplug.py paho-mqtt              (guarded import)    |
  |    modbus_codec.py  pure register decode: word order, byte      |
  |                     order, bits, scaling                        |
  +---------------------------------------------------------------+
                                 |
             Reading(tag, value, timestamp, quality, raw)
                                 |
        +------------------------+------------------------+
        v                        v                        v
  +-----------+           +-------------+          +-------------+
  | historian |           |   alarms    |          |     oee     |
  | deadband  |           | hysteresis  |          | A x P x Q   |
  | swinging  |           | on/off delay|          | downtime    |
  |   door    |           | latch/shelve|          |   Pareto    |
  | SQLite    |           | flood detect|          |             |
  +-----------+           +-------------+          +-------------+
        |                        |                        |
        +------------------------+------------------------+
                                 v
  +---------------------------------------------------------------+
  |  dashboard.py   stdlib HTTP server, one HTML file,             |
  |                 server-rendered inline SVG trends, SSE or      |
  |                 polling, alarm banner + ack buttons, OEE panel |
  +---------------------------------------------------------------+

  safety.py sits across the write path only:
      caller -> WriteGuard.check() -> Driver.write()
      read-only default / allow-list / clamp / rate limit / token
```

## The scan cycle

`Runtime.process()` fixes the order, because the order is load-bearing:

1. **Poll** the groups that are due. Each group's tags are already coalesced
   into blocks at construction time, so this is a fixed number of round trips.
2. **Archive** the readings, compressed. This happens before anything else
   looks at the values, so every value an alarm fired on is in the history.
3. **Evaluate alarms** against exactly the same values. If alarms ran first,
   an incident review could find an alarm referencing a value that was never
   archived.
4. **Log alarm transitions** into the historian's event table, next to the
   trend data they refer to.
5. **Update OEE** counters and the run/stop state from the line state tag.

## Design decisions worth stating

**One thread.** The scan loop is synchronous and single-threaded. Industrial
acquisition is easier to reason about, easier to test and much easier to hand
over when exactly one thread decides what happens next. The dashboard's HTTP
server is the only other thread, and it only reads (the historian holds a lock
and opens SQLite with `check_same_thread=False` for exactly this reason).

**One clock.** Every timing decision — poll scheduling, alarm delays,
historian timestamps, OEE windows, rate limits, token expiry — goes through an
injected clock object. In production it reads the system clock; in tests and
in `--demo` it is a `ManualClock` that only moves when it is told. That is the
difference between a test that *proves* an off-delay timer works and a test
that sleeps for two seconds and hopes.

**One address map.** Nothing outside `config/tags_bottling_line.yaml` (and its
in-code twin in `protocols/simulator.py`) knows a register address. Drivers,
poller, alarms, historian and dashboard all take `TagDef` objects.

**The simulator is not a mock.** It runs a continuous-time process model and
renders the result into a real Modbus register image, which the driver then
decodes through the real codec using each tag's configured word and byte
order. If the word-order handling breaks, the simulator tests fail. A mock
that returned canned values would not catch that.

**Optional dependencies are guarded, not required.** `pymodbus`, `asyncua`
and `paho-mqtt` are all optional. Every module imports without them; only
constructing a driver raises, and the message names the package to install.
The whole test suite runs with none of them present.

**Quality is a first-class value.** A failed read is `Quality.BAD` with
`value=None`, never `0.0`. Alarms hold their state on bad quality; the
historian refuses to archive it.

## Module map

| Module | Responsibility |
|---|---|
| `datatypes.py` | Data types, word/byte order, register areas, protocol limits. No dependencies. |
| `clock.py` | `SystemClock` and `ManualClock`. |
| `protocols/modbus_codec.py` | Pure register encode/decode. Word order, byte order, bits, scaling. |
| `protocols/base.py` | `Driver` ABC, `Reading`, `Quality`, driver statistics. |
| `protocols/simulator.py` | Bottling-line process model, register image, `SimulatorDriver`, the built-in tag map. |
| `protocols/modbus_tcp.py` | Modbus/TCP over pymodbus, with version-tolerant call signatures. |
| `protocols/modbus_rtu.py` | Modbus RTU: serial transport plus inter-frame timing. |
| `protocols/opcua.py` | OPC UA over asyncua, synchronous on the outside. |
| `protocols/mqtt_sparkplug.py` | Sparkplug B topics, birth/death certificates, report by exception. |
| `tags.py` | `TagDef`, `TagDatabase`, YAML/CSV loading, whole-map validation. |
| `poller.py` | `coalesce_blocks`, poll groups, staggering, connection health, overrun detection. |
| `alarms.py` | Alarm types, hysteresis, delays, latching, shelving, flood detection. |
| `historian.py` | `SwingingDoor`, SQLite storage, aggregation, retention, CSV export. |
| `oee.py` | `compute_oee`, `DowntimeTracker`, Pareto, report rendering. |
| `dashboard.py` | SVG rendering, JSON snapshot, HTTP handler, the single-file page. |
| `safety.py` | `WritePolicy`, `WriteGuard`, tokens, clamping, rate limiting, audit. |
| `runtime.py` | Wiring: builds and drives the whole stack; CLI output formatting. |
| `cli.py` | Subcommands, argument parsing, `--demo`. |

## Extending it

**A new protocol.** Subclass `Driver`, implement `connect`, `disconnect`,
`read(tags)` and `write(tag, value)`. Return one `Reading` per requested tag,
using `Quality.BAD` rather than omitting a tag on failure. Guard the import of
whatever library it needs and call `require()` in `__init__`. Nothing above
the driver layer needs to change.

**A new alarm type.** Add a member to `AlarmType` and a branch in
`AlarmEngine._evaluate`. Hysteresis, delays, latching, shelving and flood
detection are applied by `_update_one` and come for free.

**A different historian.** `Historian`'s public surface is `record`,
`record_readings`, `query`, `aggregate`, `apply_retention` and `export_csv`.
Implement those against InfluxDB or TimescaleDB and the rest of the stack does
not notice. Keep `SwingingDoor` — it is transport-independent.

**A new dashboard panel.** `DashboardApp.snapshot()` returns one dict, and the
page renders from it. Add a key, add a render function in the page's script.
