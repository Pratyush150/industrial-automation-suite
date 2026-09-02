"""A simulated PLC running a bottling line, plus the driver that reads it.

This is not a mock. It is a small continuous-time process model -- conveyor
speed with motor lag, a product tank that drains and refills, a chilled fill
temperature with real thermal lag, motor current that follows load, vibration,
air pressure, a capper, a counter and a state machine -- rendered into an
actual Modbus register image. The driver then reads that image back through
:mod:`factorylink.protocols.modbus_codec`, using each tag's configured word and
byte order.

So the simulator exercises the same decode path a real PLC would. If the word
order handling breaks, the simulator tests fail. That is the point.

Everything is deterministic: noise comes from a seeded PRNG advanced once per
fixed timestep, so two runs with the same seed and the same fault schedule
produce identical numbers.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any, Iterable, Sequence

from ..clock import Clock, SystemClock
from ..datatypes import DataType, RegisterArea, is_bit_area
from ..tags import TagDatabase, TagDef
from .base import ConnectionError_, Driver, Quality, Reading
from .modbus_codec import decode_registers, encode_value

__all__ = [
    "Fault",
    "LineState",
    "StopEvent",
    "ProcessConfig",
    "SimulatedPLC",
    "SimulatorDriver",
    "BOTTLING_LINE_TAGS",
    "bottling_line_tags",
    "build_simulation",
    "iter_signals",
]

HOLDING_SIZE = 128
COIL_SIZE = 32
DISCRETE_SIZE = 32


class LineState(IntEnum):
    """Coarse line state, written to a single register the way a PLC would."""

    STOPPED = 0
    RUNNING = 1
    FAULT = 2
    STARVED = 3


class Fault(str, Enum):
    """Injectable faults. Each one produces a different signature in the data."""

    NONE = "none"
    JAM = "jam"
    MOTOR_OVERLOAD = "motor_overload"
    CHILLER_FAILURE = "chiller_failure"
    LOW_TANK = "low_tank"
    AIR_LEAK = "air_leak"
    SENSOR_STUCK = "sensor_stuck"
    COMMS_DROP = "comms_drop"

    @classmethod
    def parse(cls, text: str) -> "Fault":
        """Parse a fault name from the CLI."""
        key = str(text).strip().lower().replace("-", "_")
        try:
            return cls(key)
        except ValueError:
            valid = ", ".join(m.value for m in cls if m is not cls.NONE)
            raise ValueError(f"unknown fault {text!r} (expected one of: {valid})") from None


#: Fault code written to the ``fault_code`` register, as a PLC would.
FAULT_CODES: dict[Fault, int] = {
    Fault.NONE: 0,
    Fault.JAM: 12,
    Fault.MOTOR_OVERLOAD: 21,
    Fault.CHILLER_FAILURE: 33,
    Fault.LOW_TANK: 41,
    Fault.AIR_LEAK: 52,
    Fault.SENSOR_STUCK: 64,
    Fault.COMMS_DROP: 71,
}

#: Downtime reason text per fault, used by the OEE Pareto.
FAULT_REASONS: dict[Fault, str] = {
    Fault.JAM: "Conveyor jam",
    Fault.MOTOR_OVERLOAD: "Motor overload trip",
    Fault.CHILLER_FAILURE: "Chiller failure",
    Fault.LOW_TANK: "Product starvation",
    Fault.AIR_LEAK: "Air pressure loss",
    Fault.SENSOR_STUCK: "Sensor fault",
    Fault.COMMS_DROP: "Network loss",
}


@dataclass
class StopEvent:
    """A period during which the line was not producing."""

    start: float
    end: float | None
    reason: str
    category: str = "unplanned"

    @property
    def duration(self) -> float:
        """Seconds of downtime, 0 while the stop is still open."""
        if self.end is None:
            return 0.0
        return max(0.0, self.end - self.start)


@dataclass
class ProcessConfig:
    """Physical constants of the simulated line."""

    dt: float = 0.1
    #: Conveyor speed setpoint in metres/minute when running.
    speed_setpoint: float = 30.0
    #: Conveyor first-order time constant, seconds.
    speed_tau: float = 2.0
    #: Bottles carried per metre of conveyor.
    bottles_per_metre: float = 4.0
    #: Product tank volume, litres.
    tank_volume_l: float = 2000.0
    #: Fill volume per bottle, litres.
    fill_volume_l: float = 0.5
    #: Refill valve throughput as percent of tank per second.
    refill_rate_pct_s: float = 0.9
    refill_on_below: float = 40.0
    refill_off_above: float = 90.0
    #: Chilled product setpoint and thermal time constant, seconds.
    temp_setpoint: float = 4.0
    temp_tau: float = 180.0
    #: Chiller authority: degrees of pull-down at 100% output.
    chiller_authority: float = 22.0
    ambient_mean: float = 21.0
    ambient_swing: float = 1.5
    ambient_period: float = 3600.0
    #: Motor current model, amps.
    current_idle: float = 4.0
    current_per_speed: float = 0.28
    #: Baseline reject fraction of production.
    base_reject_rate: float = 0.008
    nominal_air_pressure: float = 6.5
    nominal_head_pressure: float = 2.4
    nominal_bottle_weight_g: float = 500.0
    nominal_capper_torque: float = 1.8
    #: Ideal cycle time in seconds per bottle at the design speed.
    ideal_cycle_time: float = 0.5


class SimulatedPLC:
    """The process model plus its Modbus register image.

    Call :meth:`step` at a fixed timestep, then :meth:`sync_image` to render
    the process state into registers. :class:`SimulatorDriver` does both for
    you when it is driven by a poller.
    """

    def __init__(
        self,
        config: ProcessConfig | None = None,
        seed: int = 1234,
        start_running: bool = True,
        noise: bool = True,
    ) -> None:
        self.config = config or ProcessConfig()
        self.rng = random.Random(seed)
        self.noise_enabled = noise
        self.t = 0.0

        self.state = LineState.RUNNING if start_running else LineState.STOPPED
        self.active_fault = Fault.NONE
        self.fault_until: float | None = None
        self._schedule: list[tuple[float, Fault, float | None]] = []

        # Continuous process state.
        self.conveyor_speed = self.config.speed_setpoint if start_running else 0.0
        self.speed_command = self.config.speed_setpoint if start_running else 0.0
        self.tank_level = 82.0
        self.fill_temperature = self.config.temp_setpoint
        self.ambient_temperature = self.config.ambient_mean
        self.chiller_output = 45.0
        self.chiller_enabled = True
        self.air_pressure = self.config.nominal_air_pressure
        self.head_pressure = self.config.nominal_head_pressure
        self.motor_current = self.config.current_idle
        self.vibration_rms = 1.4
        self.capper_torque = self.config.nominal_capper_torque
        self.label_offset = 0.1
        self.bottle_weight = self.config.nominal_bottle_weight_g
        self.fill_valve_position = 0.0
        self.energy_kwh = 0.0
        self.line_rate = 0.0
        self.cycle_time = 0.0
        self.recipe_id = 7
        self.target_rate = int(
            self.config.speed_setpoint * self.config.bottles_per_metre
        )

        # Counters and bookkeeping.
        self.product_count = 0
        self.reject_count = 0
        self.runtime_seconds = 0.0
        self.downtime_seconds = 0.0
        self._bottle_accumulator = 0.0
        self._refilling = False
        self._stuck_temperature: float | None = None
        self._integral = 0.0
        self.stop_events: list[StopEvent] = []
        self.jam_detected = False
        self.estop_healthy = True
        self.guard_closed = True
        self.bottle_present = False

        # Register image.
        self.holding: list[int] = [0] * HOLDING_SIZE
        self.input_registers: list[int] = [0] * HOLDING_SIZE
        self.coils: list[bool] = [False] * COIL_SIZE
        self.discrete: list[bool] = [False] * DISCRETE_SIZE
        self.comms_ok = True

    # -- control surface ---------------------------------------------------

    def start(self) -> None:
        """Command the line to run."""
        if self.state is LineState.FAULT:
            return
        self._close_stop_event()
        self.state = LineState.RUNNING
        self.speed_command = self.config.speed_setpoint

    def stop(self, reason: str = "Operator stop", category: str = "planned") -> None:
        """Command the line to stop and open a downtime record."""
        if self.state is not LineState.RUNNING:
            return
        self.state = LineState.STOPPED
        self.speed_command = 0.0
        self.stop_events.append(StopEvent(self.t, None, reason, category))

    def inject_fault(self, fault: Fault, duration: float | None = None) -> None:
        """Raise a fault now, optionally clearing it after ``duration`` seconds."""
        if fault is Fault.NONE:
            self.clear_fault()
            return
        self.active_fault = fault
        self.fault_until = None if duration is None else self.t + duration
        self.speed_command = 0.0
        if fault is Fault.JAM:
            self.jam_detected = True
        if fault is Fault.COMMS_DROP:
            self.comms_ok = False
        if fault is Fault.SENSOR_STUCK:
            # A stuck sensor does not stop the line. That is precisely why it
            # is the most dangerous fault in here: the number looks healthy.
            self._stuck_temperature = self.fill_temperature
        if fault in (Fault.SENSOR_STUCK, Fault.COMMS_DROP):
            return
        if self.state is not LineState.FAULT:
            self.state = LineState.FAULT
            self.stop_events.append(
                StopEvent(self.t, None, FAULT_REASONS.get(fault, fault.value), "unplanned")
            )

    def schedule_fault(self, at: float, fault: Fault, duration: float | None = None) -> None:
        """Queue a fault to be injected at simulated time ``at``."""
        self._schedule.append((float(at), fault, duration))
        self._schedule.sort(key=lambda item: item[0])

    def clear_fault(self) -> None:
        """Clear the active fault and restart the line."""
        self.active_fault = Fault.NONE
        self.fault_until = None
        self.jam_detected = False
        self.comms_ok = True
        self._stuck_temperature = None
        self._close_stop_event()
        self.state = LineState.RUNNING
        self.speed_command = self.config.speed_setpoint

    def _close_stop_event(self) -> None:
        for event in reversed(self.stop_events):
            if event.end is None:
                event.end = self.t
                break

    # -- simulation --------------------------------------------------------

    def _noise(self, sigma: float) -> float:
        if not self.noise_enabled or sigma <= 0:
            return 0.0
        return self.rng.gauss(0.0, sigma)

    def step(self, dt: float | None = None) -> None:
        """Advance the process model by one timestep."""
        cfg = self.config
        step_dt = cfg.dt if dt is None else float(dt)
        if step_dt <= 0:
            raise ValueError("timestep must be positive")

        self._apply_schedule()
        if self.fault_until is not None and self.t >= self.fault_until:
            self.clear_fault()

        self._update_state_machine()
        self._update_mechanics(step_dt)
        self._update_thermal(step_dt)
        self._update_tank(step_dt)
        self._update_production(step_dt)
        self._update_counters(step_dt)

        self.t += step_dt

    def _apply_schedule(self) -> None:
        while self._schedule and self._schedule[0][0] <= self.t:
            _, fault, duration = self._schedule.pop(0)
            self.inject_fault(fault, duration)

    def _update_state_machine(self) -> None:
        if self.active_fault not in (Fault.NONE, Fault.SENSOR_STUCK, Fault.COMMS_DROP):
            self.state = LineState.FAULT
            self.speed_command = 0.0
            return
        if self.state is LineState.FAULT:
            return
        if self.tank_level < 5.0 and self.state is LineState.RUNNING:
            self.state = LineState.STARVED
            self.speed_command = 0.0
            self.stop_events.append(StopEvent(self.t, None, "Product starvation", "unplanned"))
        elif self.state is LineState.STARVED and self.tank_level > 25.0:
            self._close_stop_event()
            self.state = LineState.RUNNING
            self.speed_command = self.config.speed_setpoint

    def _update_mechanics(self, dt: float) -> None:
        cfg = self.config
        alpha = dt / (cfg.speed_tau + dt)
        self.conveyor_speed += alpha * (self.speed_command - self.conveyor_speed)
        if self.conveyor_speed < 0.02:
            self.conveyor_speed = 0.0

        load = 1.0
        if self.active_fault is Fault.JAM:
            load = 2.6
        elif self.active_fault is Fault.MOTOR_OVERLOAD:
            load = 1.0 + min(3.0, 0.55 * max(0.0, self.t - (self.fault_until or self.t) + 30.0))
        idle = cfg.current_idle if self.conveyor_speed > 0.1 else 0.0
        self.motor_current = max(
            0.0,
            idle + cfg.current_per_speed * self.conveyor_speed * load + self._noise(0.12),
        )
        if self.active_fault is Fault.MOTOR_OVERLOAD:
            self.motor_current = max(self.motor_current, 24.5 + self._noise(0.4))

        self.vibration_rms = max(
            0.0,
            1.1
            + 0.045 * self.conveyor_speed
            + (5.5 if self.active_fault is Fault.JAM else 0.0)
            + self._noise(0.18),
        )

        leak = 0.9 if self.active_fault is Fault.AIR_LEAK else 0.0
        target_air = cfg.nominal_air_pressure - leak * 3.0
        self.air_pressure += (dt / (8.0 + dt)) * (target_air - self.air_pressure)
        self.air_pressure = max(0.0, self.air_pressure + self._noise(0.03))

        running = self.state is LineState.RUNNING
        target_head = cfg.nominal_head_pressure if running else 0.3
        self.head_pressure += (dt / (3.0 + dt)) * (target_head - self.head_pressure)
        self.head_pressure = max(0.0, self.head_pressure + self._noise(0.02))

        self.capper_torque = max(
            0.0,
            cfg.nominal_capper_torque
            + 0.0004 * self.t
            + (0.9 if self.active_fault is Fault.JAM else 0.0)
            + self._noise(0.05),
        )
        self.label_offset = 0.1 + 0.00008 * self.t + self._noise(0.25)

    def _update_thermal(self, dt: float) -> None:
        cfg = self.config
        self.ambient_temperature = (
            cfg.ambient_mean
            + cfg.ambient_swing * math.sin(2.0 * math.pi * self.t / cfg.ambient_period)
            + self._noise(0.05)
        )

        if self.active_fault is Fault.CHILLER_FAILURE or not self.chiller_enabled:
            self.chiller_output = 0.0
        else:
            error = self.fill_temperature - cfg.temp_setpoint
            self._integral = max(-60.0, min(60.0, self._integral + error * dt * 0.6))
            self.chiller_output = max(0.0, min(100.0, 18.0 * error + self._integral + 45.0))

        pull_down = cfg.chiller_authority * (self.chiller_output / 100.0)
        target = self.ambient_temperature - pull_down
        alpha = dt / (cfg.temp_tau + dt)
        self.fill_temperature += alpha * (target - self.fill_temperature)
        self.fill_temperature += self._noise(0.02)

        if self.active_fault is Fault.SENSOR_STUCK:
            if self._stuck_temperature is None:
                self._stuck_temperature = self.fill_temperature
        else:
            self._stuck_temperature = None

    def _update_tank(self, dt: float) -> None:
        cfg = self.config
        if self.active_fault is Fault.LOW_TANK:
            self._refilling = False
        elif self.tank_level < cfg.refill_on_below:
            self._refilling = True
        elif self.tank_level > cfg.refill_off_above:
            self._refilling = False
        if self._refilling:
            self.tank_level = min(100.0, self.tank_level + cfg.refill_rate_pct_s * dt)

    def _update_production(self, dt: float) -> None:
        cfg = self.config
        self.line_rate = self.conveyor_speed * cfg.bottles_per_metre
        self.cycle_time = 60.0 / self.line_rate if self.line_rate > 1.0 else 0.0
        self.fill_valve_position = min(100.0, self.line_rate / 1.4) if self.line_rate else 0.0

        produced = self.line_rate / 60.0 * dt
        self._bottle_accumulator += produced
        drain_pct = 100.0 * cfg.fill_volume_l / cfg.tank_volume_l

        while self._bottle_accumulator >= 1.0:
            self._bottle_accumulator -= 1.0
            self.product_count += 1
            self.tank_level = max(0.0, self.tank_level - drain_pct)
            weight = cfg.nominal_bottle_weight_g + self._noise(3.0)
            if self.fill_temperature > 8.0:
                weight -= 6.0
            if self.air_pressure < 5.0:
                weight -= 9.0
            self.bottle_weight = weight
            reject_p = cfg.base_reject_rate
            if self.vibration_rms > 7.0:
                reject_p += 0.05
            if abs(weight - cfg.nominal_bottle_weight_g) > 12.0:
                reject_p += 0.5
            if self.rng.random() < reject_p:
                self.reject_count += 1
            self.bottle_present = True

    def _update_counters(self, dt: float) -> None:
        if self.state is LineState.RUNNING:
            self.runtime_seconds += dt
        else:
            self.downtime_seconds += dt
        self.energy_kwh += self.motor_current * 400.0 * math.sqrt(3.0) * dt / 3.6e6

    # -- outputs -----------------------------------------------------------

    @property
    def good_count(self) -> int:
        """Products that passed inspection."""
        return self.product_count - self.reject_count

    @property
    def reported_temperature(self) -> float:
        """Fill temperature as the *sensor* reports it, stuck faults included."""
        if self._stuck_temperature is not None:
            return self._stuck_temperature
        return self.fill_temperature

    def status_bits(self) -> dict[str, bool]:
        """The individual bits packed into the status word."""
        return {
            "sts_running": self.state is LineState.RUNNING,
            "sts_fault": self.state is LineState.FAULT,
            "sts_estop_ok": self.estop_healthy,
            "sts_low_level": self.tank_level < 25.0,
            "sts_jam": self.jam_detected,
        }

    def values(self) -> dict[str, float | bool]:
        """Every simulated signal, keyed by tag name, in engineering units."""
        out: dict[str, float | bool] = {
            "conveyor_speed": self.conveyor_speed,
            "conveyor_speed_sp": self.speed_command,
            "motor_current": self.motor_current,
            "tank_level": self.tank_level,
            "fill_temperature": self.reported_temperature,
            "ambient_temperature": self.ambient_temperature,
            "chiller_output": self.chiller_output,
            "fill_head_pressure": self.head_pressure,
            "fill_valve_position": self.fill_valve_position,
            "bottle_weight": self.bottle_weight,
            "product_count": float(self.product_count),
            "reject_count": float(self.reject_count),
            "good_count": float(self.good_count),
            "line_state": float(int(self.state)),
            "fault_code": float(FAULT_CODES.get(self.active_fault, 0)),
            "cycle_time": self.cycle_time,
            "runtime_seconds": self.runtime_seconds,
            "downtime_seconds": self.downtime_seconds,
            "vibration_rms": self.vibration_rms,
            "air_pressure": self.air_pressure,
            "capper_torque": self.capper_torque,
            "label_offset": self.label_offset,
            "energy_kwh": self.energy_kwh,
            "recipe_id": float(self.recipe_id),
            "target_rate": float(self.target_rate),
            "line_rate": self.line_rate,
            "cmd_start": self.state is LineState.RUNNING,
            "cmd_stop": self.state is LineState.STOPPED,
            "cmd_reset_fault": False,
            "cmd_chiller_enable": self.chiller_enabled,
            "di_bottle_present": self.bottle_present,
            "di_guard_closed": self.guard_closed,
            "di_estop_healthy": self.estop_healthy,
        }
        out.update(self.status_bits())
        return out

    # -- register image ----------------------------------------------------

    def _image_for(self, area: RegisterArea) -> list[Any]:
        if area is RegisterArea.HOLDING:
            return self.holding
        if area is RegisterArea.INPUT:
            return self.input_registers
        if area is RegisterArea.COIL:
            return self.coils
        return self.discrete

    def sync_image(self, db: TagDatabase) -> None:
        """Render the process state into the register image via the codec.

        Numeric tags are encoded first; bit tags are then OR-ed into their
        status word so several bits can share one register the way they do on
        a real controller.
        """
        values = self.values()
        bit_tags: list[TagDef] = []
        for tag in db:
            if tag.name not in values:
                continue
            value = values[tag.name]
            image = self._image_for(tag.area)
            if is_bit_area(tag.area):
                image[tag.address] = bool(value)
                continue
            if tag.bit is not None:
                bit_tags.append(tag)
                continue
            raw = tag.to_raw(value)
            regs = encode_value(raw, tag.data_type, tag.word_order, tag.byte_order)
            if tag.address + len(regs) > len(image):
                raise IndexError(f"tag {tag.name} at {tag.address} is outside the register image")
            image[tag.address : tag.address + len(regs)] = regs

        words: dict[tuple[RegisterArea, int], int] = {}
        for tag in bit_tags:
            key = (tag.area, tag.address)
            base = words.get(key, 0)
            words[key] = encode_value(
                bool(values[tag.name]),
                DataType.BOOL,
                tag.word_order,
                tag.byte_order,
                bit=tag.bit,
                base_word=base,
            )[0]
        for (area, address), word in words.items():
            self._image_for(area)[address] = word

    def read_registers(self, area: RegisterArea, address: int, count: int) -> list[Any]:
        """Read a raw span out of the register image, as a device would."""
        image = self._image_for(area)
        if address < 0 or address + count > len(image):
            raise IndexError(
                f"read of {count} at {address} in {area.value} is outside the image "
                f"(size {len(image)})"
            )
        return list(image[address : address + count])

    def write_register(self, area: RegisterArea, address: int, value: Any) -> None:
        """Write a single raw register or coil into the image."""
        image = self._image_for(area)
        if address < 0 or address >= len(image):
            raise IndexError(f"write to {address} in {area.value} is outside the image")
        image[address] = value


class SimulatorDriver(Driver):
    """A :class:`~factorylink.protocols.base.Driver` over :class:`SimulatedPLC`.

    Reads go through the real register image and the real codec, so word order,
    byte order, scaling and bit extraction are all exercised end to end.

    ``latency_s`` advances the injected clock on every read, which is how the
    poller's slow-scan detection is tested without sleeping.
    """

    protocol = "simulator"

    def __init__(
        self,
        plc: SimulatedPLC,
        db: TagDatabase,
        clock: Clock | None = None,
        device: str = "line1",
        latency_s: float = 0.0,
        auto_step: bool = True,
    ) -> None:
        super().__init__(device=device)
        self.plc = plc
        self.db = db
        self.clock = clock or SystemClock()
        self.latency_s = float(latency_s)
        self.auto_step = auto_step
        self._last_step_time: float | None = None

    def connect(self) -> None:
        """Mark the simulated transport as up."""
        if not self._connected:
            self._connected = True
            self.stats.connects += 1

    def disconnect(self) -> None:
        """Mark the simulated transport as down."""
        if self._connected:
            self._connected = False
            self.stats.disconnects += 1

    def advance_to(self, timestamp: float) -> None:
        """Run the process model forward to ``timestamp`` in fixed steps."""
        if self._last_step_time is None:
            self._last_step_time = timestamp
            self.plc.sync_image(self.db)
            return
        dt = self.plc.config.dt
        steps = int(max(0.0, timestamp - self._last_step_time) / dt)
        for _ in range(steps):
            self.plc.step(dt)
        self._last_step_time += steps * dt
        self.plc.sync_image(self.db)

    def read(self, tags: Sequence[TagDef]) -> dict[str, Reading]:
        """Decode ``tags`` out of the simulated register image."""
        if not self._connected:
            raise ConnectionError_(f"{self.device}: not connected")
        if self.latency_s:
            self.clock.sleep(self.latency_s)
        now = self.clock.now()
        if self.auto_step:
            self.advance_to(now)
        self.stats.reads += 1
        self.stats.last_read_duration = self.latency_s

        if not self.plc.comms_ok:
            self.stats.read_failures += 1
            self.stats.last_error = "simulated network loss"
            return self.bad_readings(tags, "simulated network loss", now)

        out: dict[str, Reading] = {}
        for tag in tags:
            try:
                out[tag.name] = self._read_one(tag, now)
            except Exception as exc:  # noqa: BLE001 - reported as BAD quality
                self.stats.read_failures += 1
                self.stats.last_error = str(exc)
                out[tag.name] = Reading(tag.name, None, now, Quality.BAD, error=str(exc))
        return out

    def _read_one(self, tag: TagDef, now: float) -> Reading:
        if is_bit_area(tag.area):
            raw = self.plc.read_registers(tag.area, tag.address, 1)
            return Reading(tag.name, bool(raw[0]), now, Quality.GOOD, raw=(int(bool(raw[0])),))
        regs = self.plc.read_registers(tag.area, tag.address, tag.register_count)
        decoded = decode_registers(regs, tag.data_type, tag.word_order, tag.byte_order, tag.bit)
        value = tag.to_engineering(decoded)
        return Reading(tag.name, value, now, Quality.GOOD, raw=tuple(int(r) for r in regs))

    def write(self, tag: TagDef, value: float | bool) -> None:
        """Write an engineering value into the simulated register image."""
        if not self._connected:
            raise ConnectionError_(f"{self.device}: not connected")
        self.stats.writes += 1
        if is_bit_area(tag.area):
            self.plc.write_register(tag.area, tag.address, bool(value))
            self._apply_command(tag.name, bool(value))
            return
        raw = tag.to_raw(value)
        if tag.bit is not None:
            base = self.plc.read_registers(tag.area, tag.address, 1)[0]
            word = encode_value(
                bool(value), DataType.BOOL, tag.word_order, tag.byte_order, tag.bit, base
            )[0]
            self.plc.write_register(tag.area, tag.address, word)
            return
        regs = encode_value(raw, tag.data_type, tag.word_order, tag.byte_order)
        for offset, reg in enumerate(regs):
            self.plc.write_register(tag.area, tag.address + offset, reg)
        self._apply_command(tag.name, float(value))

    def _apply_command(self, name: str, value: float | bool) -> None:
        """Let a few writes actually influence the process, as they would."""
        plc = self.plc
        if name == "cmd_start" and value:
            plc.start()
        elif name == "cmd_stop" and value:
            plc.stop()
        elif name == "cmd_reset_fault" and value:
            plc.clear_fault()
        elif name == "cmd_chiller_enable":
            plc.chiller_enabled = bool(value)
        elif name == "conveyor_speed_sp":
            plc.speed_command = float(value)
            plc.config.speed_setpoint = float(value)
        elif name == "target_rate":
            plc.target_rate = int(value)


def _tag(
    name: str,
    address: int,
    data_type: str,
    unit: str = "",
    description: str = "",
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": name,
        "device": "line1",
        "address": address,
        "data_type": data_type,
        "unit": unit,
        "description": description,
    }
    row.update(extra)
    return row


#: The bottling line address map. ``config/tags_bottling_line.yaml`` is
#: generated from this list, so the file and the code can never drift.
#: Word and byte orders are deliberately mixed: a real plant is a mix of
#: vendors and the map is where that mess is recorded once.
BOTTLING_LINE_TAGS: list[dict[str, Any]] = [
    _tag("conveyor_speed", 0, "float32", "m/min", "Conveyor line speed",
         poll_group="fast", min_value=0, max_value=120, deadband=0.1,
         alarm={"hi": 34.0, "hi_hi": 40.0, "deadband": 1.0}),
    _tag("conveyor_speed_sp", 2, "float32", "m/min", "Conveyor speed setpoint",
         poll_group="slow", writable=True, min_value=0, max_value=45),
    _tag("motor_current", 4, "float32", "A", "Conveyor motor RMS current",
         poll_group="fast", word_order="little", min_value=0, max_value=60, deadband=0.1,
         alarm={"hi": 18.0, "hi_hi": 24.0, "deadband": 0.8}),
    _tag("tank_level", 6, "float32", "%", "Product tank level",
         poll_group="normal", min_value=0, max_value=100, deadband=0.2,
         alarm={"lo": 25.0, "lo_lo": 10.0, "deadband": 2.0}),
    _tag("fill_temperature", 8, "float32", "degC", "Chilled product temperature at the filler",
         poll_group="normal", min_value=-10, max_value=60, deadband=0.05,
         alarm={"hi": 8.0, "hi_hi": 12.0, "lo": 1.5, "lo_lo": 0.5, "deadband": 0.4}),
    _tag("ambient_temperature", 10, "float32", "degC", "Filling room ambient temperature",
         poll_group="slow", min_value=-20, max_value=60, deadband=0.2),
    _tag("chiller_output", 12, "float32", "%", "Chiller control output",
         poll_group="normal", min_value=0, max_value=100, deadband=1.0),
    _tag("fill_head_pressure", 14, "float32", "bar", "Fill head supply pressure",
         poll_group="fast", min_value=0, max_value=10, deadband=0.02,
         alarm={"hi": 3.2, "hi_hi": 3.8, "lo": 1.2, "deadband": 0.15}),
    _tag("fill_valve_position", 16, "uint16", "%", "Fill valve demand",
         poll_group="fast", scale=0.1, min_value=0, max_value=100, deadband=0.5),
    _tag("bottle_weight", 17, "uint16", "g", "Last filled bottle net weight",
         poll_group="fast", scale=0.5, min_value=0, max_value=2000, deadband=1.0,
         alarm={"lo": 488.0, "lo_lo": 478.0, "hi": 512.0, "hi_hi": 522.0, "deadband": 2.0}),
    _tag("product_count", 18, "uint32", "bottles", "Total bottles produced this shift",
         poll_group="fast", min_value=0, deadband=0.5),
    _tag("reject_count", 20, "uint32", "bottles", "Bottles rejected this shift",
         poll_group="fast", min_value=0, deadband=0.5),
    _tag("good_count", 22, "uint32", "bottles", "Bottles accepted this shift",
         poll_group="fast", min_value=0, deadband=0.5),
    _tag("line_state", 24, "uint16", "", "0 stopped, 1 running, 2 fault, 3 starved",
         poll_group="fast", min_value=0, max_value=3),
    _tag("fault_code", 25, "uint16", "", "Active fault code, 0 when healthy",
         poll_group="fast", min_value=0),
    _tag("sts_running", 26, "bool", "", "Status word bit 0: line running",
         poll_group="fast", bit=0),
    _tag("sts_fault", 26, "bool", "", "Status word bit 1: fault latched",
         poll_group="fast", bit=1),
    _tag("sts_estop_ok", 26, "bool", "", "Status word bit 2: E-stop circuit healthy",
         poll_group="fast", bit=2),
    _tag("sts_low_level", 26, "bool", "", "Status word bit 3: tank low level switch",
         poll_group="fast", bit=3),
    _tag("sts_jam", 26, "bool", "", "Status word bit 7: conveyor jam sensor",
         poll_group="fast", bit=7),
    _tag("cycle_time", 27, "uint16", "s", "Measured cycle time per bottle",
         poll_group="fast", scale=0.001, min_value=0, max_value=60, deadband=0.005),
    _tag("runtime_seconds", 28, "uint32", "s", "Accumulated running time",
         poll_group="slow", min_value=0, deadband=1.0),
    _tag("downtime_seconds", 30, "uint32", "s", "Accumulated stopped time",
         poll_group="slow", min_value=0, deadband=1.0),
    _tag("vibration_rms", 32, "float32", "mm/s", "Conveyor drive vibration, ISO 10816 RMS",
         poll_group="normal", byte_order="little", min_value=0, max_value=50, deadband=0.1,
         alarm={"hi": 7.1, "hi_hi": 11.2, "deadband": 0.6}),
    _tag("air_pressure", 34, "float32", "bar", "Plant compressed air header pressure",
         poll_group="normal", min_value=0, max_value=12, deadband=0.05,
         alarm={"lo": 5.5, "lo_lo": 4.5, "deadband": 0.25}),
    _tag("capper_torque", 36, "int16", "N.m", "Capper applied torque, last bottle",
         poll_group="fast", scale=0.01, min_value=-50, max_value=50, deadband=0.02,
         alarm={"hi": 2.5, "hi_hi": 3.0, "lo": 1.0, "deadband": 0.15}),
    _tag("label_offset", 37, "int16", "mm", "Label placement offset from datum",
         poll_group="normal", scale=0.1, min_value=-100, max_value=100, deadband=0.2,
         alarm={"hi": 2.0, "lo": -2.0, "deadband": 0.4}),
    _tag("energy_kwh", 38, "float32", "kWh", "Drive energy consumed this shift",
         poll_group="slow", min_value=0, deadband=0.005),
    _tag("recipe_id", 40, "uint16", "", "Active recipe number", poll_group="slow", min_value=0),
    _tag("target_rate", 41, "uint16", "bottles/min", "Target production rate",
         poll_group="slow", writable=True, min_value=0, max_value=600),
    _tag("line_rate", 42, "float32", "bottles/min", "Instantaneous production rate",
         poll_group="fast", min_value=0, max_value=600, deadband=0.5),
    _tag("cmd_start", 0, "bool", "", "Start command coil",
         area="coil", poll_group="normal", writable=True),
    _tag("cmd_stop", 1, "bool", "", "Stop command coil",
         area="coil", poll_group="normal", writable=True),
    _tag("cmd_reset_fault", 2, "bool", "", "Fault reset coil",
         area="coil", poll_group="normal", writable=True),
    _tag("cmd_chiller_enable", 3, "bool", "", "Chiller enable coil",
         area="coil", poll_group="normal", writable=True),
    _tag("di_bottle_present", 0, "bool", "", "Bottle-present photo-eye at the filler",
         area="discrete", poll_group="fast"),
    _tag("di_guard_closed", 1, "bool", "", "Machine guard door closed",
         area="discrete", poll_group="fast"),
    _tag("di_estop_healthy", 2, "bool", "", "E-stop relay healthy",
         area="discrete", poll_group="fast"),
]


def bottling_line_tags() -> TagDatabase:
    """Return the built-in, validated bottling-line tag database."""
    return TagDatabase.from_dicts(BOTTLING_LINE_TAGS)


def build_simulation(
    clock: Clock | None = None,
    seed: int = 1234,
    db: TagDatabase | None = None,
    config: ProcessConfig | None = None,
    latency_s: float = 0.0,
) -> tuple[SimulatedPLC, SimulatorDriver, TagDatabase]:
    """Convenience factory: PLC, connected driver and tag database."""
    tag_db = db or bottling_line_tags()
    plc = SimulatedPLC(config=config, seed=seed)
    driver = SimulatorDriver(plc, tag_db, clock=clock, latency_s=latency_s)
    driver.connect()
    plc.sync_image(tag_db)
    return plc, driver, tag_db


def iter_signals(plc: SimulatedPLC, duration: float, dt: float | None = None) -> Iterable[dict]:
    """Step the model for ``duration`` seconds, yielding the signal dict."""
    step = dt or plc.config.dt
    steps = int(duration / step)
    for _ in range(steps):
        plc.step(step)
        yield {"t": plc.t, **plc.values()}
