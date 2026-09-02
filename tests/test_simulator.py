"""The simulated PLC and its driver, including the end-to-end decode path."""

from __future__ import annotations

import pytest

from factorylink.datatypes import DataType, RegisterArea, WordOrder
from factorylink.protocols.base import ConnectionError_, Quality
from factorylink.protocols.modbus_codec import decode_registers
from factorylink.protocols.simulator import (
    Fault,
    LineState,
    ProcessConfig,
    SimulatedPLC,
    SimulatorDriver,
    bottling_line_tags,
    build_simulation,
    iter_signals,
)


def run(plc: SimulatedPLC, seconds: float) -> None:
    """Advance the model by ``seconds`` at its configured timestep."""
    for _ in range(int(seconds / plc.config.dt)):
        plc.step()


# -- the register image ----------------------------------------------------


def test_reads_go_through_the_real_register_image(plc, db, driver):
    """A read decodes registers; it does not return the model's attributes."""
    run(plc, 30.0)
    plc.sync_image(db)
    reading = driver.read([db["conveyor_speed"]])["conveyor_speed"]
    assert reading.raw is not None
    assert len(reading.raw) == 2
    decoded = decode_registers(list(reading.raw), DataType.FLOAT32, WordOrder.BIG)
    assert reading.value == pytest.approx(decoded)
    assert reading.value == pytest.approx(plc.conveyor_speed, abs=1e-4)


def test_a_little_word_order_tag_decodes_correctly(plc, db, driver):
    """motor_current is CDAB in the shipped map; prove the swap is applied."""
    run(plc, 30.0)
    plc.sync_image(db)
    reading = driver.read([db["motor_current"]])["motor_current"]
    assert db["motor_current"].word_order is WordOrder.LITTLE
    assert reading.value == pytest.approx(plc.motor_current, abs=1e-4)
    wrong = decode_registers(list(reading.raw), DataType.FLOAT32, WordOrder.BIG)
    assert wrong != pytest.approx(plc.motor_current, abs=1e-3)


def test_a_swapped_byte_order_tag_decodes_correctly(plc, db, driver):
    """vibration_rms is BADC in the shipped map."""
    run(plc, 30.0)
    plc.sync_image(db)
    reading = driver.read([db["vibration_rms"]])["vibration_rms"]
    assert reading.value == pytest.approx(plc.vibration_rms, abs=1e-4)


def test_scaled_integer_tags_decode_to_engineering_units(plc, db, driver):
    """bottle_weight is a uint16 with a 0.5 scale."""
    run(plc, 30.0)
    plc.sync_image(db)
    readings = driver.read([db["bottle_weight"], db["capper_torque"], db["cycle_time"]])
    assert readings["bottle_weight"].value == pytest.approx(plc.bottle_weight, abs=0.5)
    assert readings["capper_torque"].value == pytest.approx(plc.capper_torque, abs=0.01)
    assert readings["cycle_time"].value == pytest.approx(0.5, abs=0.001)


def test_status_bits_share_one_register(plc, db, driver):
    """Five booleans in one word, each read from its own bit."""
    run(plc, 5.0)
    plc.sync_image(db)
    names = ["sts_running", "sts_fault", "sts_estop_ok", "sts_low_level", "sts_jam"]
    readings = driver.read([db[n] for n in names])
    assert readings["sts_running"].value is True
    assert readings["sts_fault"].value is False
    assert readings["sts_estop_ok"].value is True
    word = plc.read_registers(RegisterArea.HOLDING, 26, 1)[0]
    assert word & 0b101 == 0b101


def test_coils_and_discrete_inputs_are_separate_spaces(plc, db, driver):
    """Coil 0 and discrete input 0 are different data."""
    run(plc, 5.0)
    plc.sync_image(db)
    readings = driver.read([db["cmd_start"], db["di_estop_healthy"], db["di_guard_closed"]])
    assert readings["cmd_start"].value is True
    assert readings["di_estop_healthy"].value is True
    assert readings["di_guard_closed"].value is True


def test_reading_the_whole_map_yields_good_quality(plc, db, driver):
    """Every tag in the shipped map must decode without an error."""
    run(plc, 20.0)
    plc.sync_image(db)
    readings = driver.read(db.tags())
    assert len(readings) == len(db)
    assert all(r.quality is Quality.GOOD for r in readings.values())
    assert all(r.value is not None for r in readings.values())


def test_a_read_outside_the_image_is_reported_as_bad_quality(plc, db, driver):
    """A bad address must degrade one tag, not kill the scan."""
    from dataclasses import replace

    broken = replace(db["conveyor_speed"], address=10_000)
    readings = driver.read([broken])
    assert readings["conveyor_speed"].quality is Quality.BAD
    assert "outside the image" in readings["conveyor_speed"].error


def test_reading_while_disconnected_raises(plc, db, clock):
    """The poller relies on this to drive its connection health."""
    drv = SimulatorDriver(plc, db, clock=clock)
    with pytest.raises(ConnectionError_):
        drv.read(db.tags())


# -- the process model -----------------------------------------------------


def test_the_line_produces_at_the_design_rate(plc):
    """30 m/min at 4 bottles/m is 120 bottles/min, i.e. 2 per second."""
    run(plc, 60.0)
    assert plc.state is LineState.RUNNING
    assert plc.line_rate == pytest.approx(120.0, rel=0.02)
    assert 115 <= plc.product_count <= 125
    assert plc.cycle_time == pytest.approx(0.5, rel=0.02)


def test_conveyor_speed_has_first_order_lag(plc):
    """A motor does not step to setpoint; the model must not either."""
    plc.stop()
    speeds = []
    for _ in range(30):
        plc.step()
        speeds.append(plc.conveyor_speed)
    assert speeds[0] > speeds[-1]
    assert all(a >= b for a, b in zip(speeds, speeds[1:]))
    assert speeds[0] < plc.config.speed_setpoint


def test_temperature_has_thermal_lag(plc):
    """A chiller failure takes minutes to show, not milliseconds."""
    plc.inject_fault(Fault.CHILLER_FAILURE)
    start = plc.fill_temperature
    run(plc, 10.0)
    after_10s = plc.fill_temperature
    run(plc, 600.0)
    after_10min = plc.fill_temperature
    assert after_10s - start < 1.5
    assert after_10min > start + 5.0


def test_the_tank_drains_as_bottles_are_filled(plc):
    """0.5 L out of a 2000 L tank per bottle."""
    start = plc.tank_level
    run(plc, 120.0)
    drained = start - plc.tank_level
    assert drained == pytest.approx(plc.product_count * 0.025, abs=0.05)


def test_the_tank_refills_below_the_setpoint(plc):
    """Level control keeps the line from starving on its own."""
    plc.tank_level = 20.0
    run(plc, 60.0)
    assert plc.tank_level > 25.0


def test_motor_current_follows_load(plc):
    """Current is idle plus a term proportional to speed."""
    run(plc, 30.0)
    running_current = plc.motor_current
    plc.stop()
    run(plc, 60.0)
    assert plc.motor_current < running_current * 0.3


# -- faults ----------------------------------------------------------------


def test_a_jam_stops_the_line_and_sets_the_bit(plc, db, driver):
    """The jam signature: state FAULT, jam bit set, fault code 12."""
    run(plc, 30.0)
    plc.inject_fault(Fault.JAM)
    run(plc, 20.0)
    plc.sync_image(db)
    readings = driver.read([db["sts_jam"], db["line_state"], db["fault_code"]])
    assert plc.state is LineState.FAULT
    assert readings["sts_jam"].value is True
    assert readings["line_state"].value == float(int(LineState.FAULT))
    assert readings["fault_code"].value == 12.0
    assert plc.conveyor_speed < 1.0


def test_a_scheduled_fault_clears_itself_and_records_downtime(plc):
    """Faults with a duration produce a closed downtime event."""
    plc.schedule_fault(30.0, Fault.JAM, 60.0)
    run(plc, 200.0)
    assert plc.active_fault is Fault.NONE
    assert plc.state is LineState.RUNNING
    assert len(plc.stop_events) == 1
    assert plc.stop_events[0].duration == pytest.approx(60.0, abs=plc.config.dt * 2)
    assert plc.stop_events[0].reason == "Conveyor jam"


def test_motor_overload_pushes_current_over_the_limit(plc, db, driver):
    """The overload signature is visible in the current tag."""
    run(plc, 20.0)
    plc.inject_fault(Fault.MOTOR_OVERLOAD)
    run(plc, 10.0)
    plc.sync_image(db)
    assert driver.read([db["motor_current"]])["motor_current"].value > 24.0


def test_low_tank_starves_the_line(plc):
    """Disabling the refill valve eventually stops production."""
    plc.tank_level = 6.0
    plc.inject_fault(Fault.LOW_TANK)
    plc.clear_fault()
    plc._refilling = False
    plc.inject_fault(Fault.LOW_TANK)
    plc.active_fault = Fault.LOW_TANK
    run(plc, 60.0)
    assert plc.tank_level <= 6.0


def test_a_stuck_sensor_freezes_the_reported_value(plc, db, driver):
    """The nastiest fault: the number looks fine and never changes."""
    run(plc, 60.0)
    plc.inject_fault(Fault.SENSOR_STUCK)
    frozen = plc.reported_temperature
    run(plc, 600.0)
    assert plc.reported_temperature == frozen
    assert plc.fill_temperature != frozen
    plc.sync_image(db)
    assert driver.read([db["fill_temperature"]])["fill_temperature"].value == pytest.approx(frozen)


def test_a_comms_drop_produces_bad_quality_not_zeros(plc, db, driver):
    """Network loss must be distinguishable from a real value of zero."""
    run(plc, 10.0)
    plc.inject_fault(Fault.COMMS_DROP)
    plc.sync_image(db)
    readings = driver.read(db.tags())
    assert all(r.quality is Quality.BAD for r in readings.values())
    assert all(r.value is None for r in readings.values())


def test_fault_names_parse_from_the_cli_form():
    """`--fault motor-overload@120` has to work."""
    assert Fault.parse("motor-overload") is Fault.MOTOR_OVERLOAD
    assert Fault.parse("JAM") is Fault.JAM
    with pytest.raises(ValueError, match="unknown fault"):
        Fault.parse("gremlins")


# -- writes and determinism ------------------------------------------------


def test_writing_a_setpoint_changes_the_process(plc, db, driver):
    """A write must actually reach the model, not just the register image."""
    driver.write(db["conveyor_speed_sp"], 15.0)
    run(plc, 30.0)
    assert plc.conveyor_speed == pytest.approx(15.0, rel=0.05)


def test_writing_a_coil_starts_and_stops_the_line(plc, db, driver):
    """Command coils are the write path an operator would use."""
    driver.write(db["cmd_stop"], True)
    assert plc.state is LineState.STOPPED
    driver.write(db["cmd_start"], True)
    assert plc.state is LineState.RUNNING


def test_the_same_seed_gives_the_same_numbers():
    """Determinism is what makes the whole test suite possible."""
    first = SimulatedPLC(seed=99)
    second = SimulatedPLC(seed=99)
    run(first, 100.0)
    run(second, 100.0)
    assert first.values() == second.values()


def test_different_seeds_give_different_noise():
    """The noise is real noise, not a constant."""
    first = SimulatedPLC(seed=1)
    second = SimulatedPLC(seed=2)
    run(first, 100.0)
    run(second, 100.0)
    assert first.motor_current != second.motor_current


def test_noise_can_be_disabled_for_exact_arithmetic():
    """Some checks are easier without noise in the way."""
    plc = SimulatedPLC(noise=False)
    run(plc, 60.0)
    assert plc.conveyor_speed == pytest.approx(30.0, abs=1e-6)


def test_build_simulation_returns_a_ready_stack(clock):
    """One call for a connected driver over the shipped tag map."""
    plc, drv, db = build_simulation(clock=clock, seed=7)
    assert drv.is_connected is True
    assert len(db) == 38
    assert drv.read([db["tank_level"]])["tank_level"].quality is Quality.GOOD


def test_iter_signals_yields_a_named_dict(plc):
    """Used by examples to dump the process to CSV."""
    frames = list(iter_signals(plc, duration=1.0))
    assert len(frames) == 10
    assert "conveyor_speed" in frames[0]
    assert frames[-1]["t"] > frames[0]["t"]


def test_the_builtin_tag_map_validates_and_covers_every_signal():
    """Every simulated signal must have a tag, and vice versa."""
    db = bottling_line_tags()
    plc = SimulatedPLC()
    signals = set(plc.values())
    assert signals == set(db.names)
    assert len(db) == 38


def test_process_config_timestep_must_be_positive(plc):
    """A zero timestep would loop forever."""
    with pytest.raises(ValueError, match="timestep"):
        plc.step(0.0)


def test_config_is_adjustable():
    """The model is parameterised, not hard-coded."""
    plc = SimulatedPLC(config=ProcessConfig(speed_setpoint=15.0, bottles_per_metre=2.0))
    run(plc, 60.0)
    assert plc.line_rate == pytest.approx(30.0, rel=0.05)
