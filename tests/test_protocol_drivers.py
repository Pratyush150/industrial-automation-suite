"""The optional-dependency drivers: guarded imports and their pure logic.

pymodbus, asyncua and paho-mqtt are all optional. These tests prove that every
module still imports, that constructing a driver without its library produces
an actionable message rather than an ImportError traceback, and that the parts
that do not need a library -- serial timing, node-id mapping, Sparkplug topics
and report-by-exception -- are correct.
"""

from __future__ import annotations

import json

import pytest

from factorylink.protocols import modbus_rtu, modbus_tcp, mqtt_sparkplug, opcua
from factorylink.protocols.base import (
    Driver,
    ModbusEndpoint,
    OptionalDependencyMissing,
    Quality,
    Reading,
    require,
)
from factorylink.protocols.mqtt_sparkplug import (
    SparkplugConfig,
    SparkplugPublisher,
    next_seq,
    sparkplug_topic,
)
from factorylink.protocols.opcua import node_id_for


def test_all_driver_modules_import_without_their_libraries():
    """Import must never depend on an optional package being present."""
    assert hasattr(modbus_tcp, "ModbusTcpDriver")
    assert hasattr(modbus_rtu, "ModbusRtuDriver")
    assert hasattr(opcua, "OpcUaDriver")
    assert hasattr(mqtt_sparkplug, "SparkplugPublisher")
    assert isinstance(modbus_tcp.PYMODBUS_AVAILABLE, bool)
    assert isinstance(opcua.ASYNCUA_AVAILABLE, bool)
    assert isinstance(mqtt_sparkplug.MQTT_AVAILABLE, bool)


def test_missing_dependency_message_names_the_package():
    """"pip install pymodbus" is more useful than a NameError."""
    with pytest.raises(OptionalDependencyMissing) as excinfo:
        require(None, "ModbusTcpDriver", "pymodbus")
    message = str(excinfo.value)
    assert "pip install pymodbus" in message
    assert "simulator" in message


@pytest.mark.skipif(modbus_tcp.PYMODBUS_AVAILABLE, reason="pymodbus is installed here")
def test_constructing_a_modbus_driver_without_pymodbus_is_explicit():
    """The failure has to happen at construction, with a fixable message."""
    with pytest.raises(OptionalDependencyMissing, match="pymodbus"):
        modbus_tcp.ModbusTcpDriver()
    with pytest.raises(OptionalDependencyMissing, match="pymodbus"):
        modbus_rtu.ModbusRtuDriver()


@pytest.mark.skipif(opcua.ASYNCUA_AVAILABLE, reason="asyncua is installed here")
def test_constructing_an_opcua_driver_without_asyncua_is_explicit():
    """Same contract for OPC UA."""
    with pytest.raises(OptionalDependencyMissing, match="asyncua"):
        opcua.OpcUaDriver()


# -- Modbus RTU timing -----------------------------------------------------


def test_character_time_matches_the_8n1_frame():
    """11 bits per character at 9600 baud is 1.1458 ms."""
    assert modbus_rtu.char_time_seconds(9600) == pytest.approx(11 / 9600)
    assert modbus_rtu.char_time_seconds(19200) == pytest.approx(11 / 19200)
    with pytest.raises(ValueError, match="baudrate"):
        modbus_rtu.char_time_seconds(0)


def test_inter_frame_gap_is_three_and_a_half_characters_below_19200():
    """4.01 ms at 9600 baud, which is why a 16 ms latency timer breaks RTU."""
    assert modbus_rtu.frame_gap_seconds(9600) == pytest.approx(3.5 * 11 / 9600)
    assert modbus_rtu.frame_gap_seconds(9600) * 1000 == pytest.approx(4.01, abs=0.01)


def test_inter_frame_gap_is_fixed_above_19200():
    """The specification pins it at 1.75 ms rather than chasing microseconds."""
    assert modbus_rtu.frame_gap_seconds(38400) == pytest.approx(0.00175)
    assert modbus_rtu.frame_gap_seconds(115200) == pytest.approx(0.00175)
    assert modbus_rtu.frame_gap_seconds(19200) > modbus_rtu.frame_gap_seconds(38400)


def test_modbus_endpoint_defaults_are_sane():
    """Port 502, unit 1, one second timeout."""
    endpoint = ModbusEndpoint()
    assert endpoint.port == 502
    assert endpoint.unit_id == 1
    assert endpoint.timeout == 1.0
    assert endpoint.baudrate == 9600


# -- OPC UA node mapping ---------------------------------------------------


def test_node_id_defaults_to_a_string_identifier(db):
    """Tag name becomes a string node id in the configured namespace."""
    assert node_id_for(db["tank_level"]) == "ns=2;s=tank_level"
    assert node_id_for(db["tank_level"], namespace=4) == "ns=4;s=tank_level"


def test_an_explicit_node_id_in_the_description_wins():
    """The escape hatch for servers with awkward identifiers."""
    from factorylink.tags import TagDef

    tag = TagDef(name="x", address=0, description="ns=3;i=1042 spare text")
    assert node_id_for(tag) == "ns=3;i=1042"


# -- Sparkplug B -----------------------------------------------------------


def test_topic_namespace_follows_the_specification():
    """spBv1.0/<group>/<type>/<edge node>/<device>."""
    assert sparkplug_topic("factory", "DDATA", "edge1", "line1") == (
        "spBv1.0/factory/DDATA/edge1/line1"
    )
    assert sparkplug_topic("factory", "NBIRTH", "edge1") == "spBv1.0/factory/NBIRTH/edge1"


def test_illegal_topic_elements_are_rejected():
    """A wildcard in a group id would subscribe to the whole plant."""
    with pytest.raises(ValueError, match="message type"):
        sparkplug_topic("factory", "DATA", "edge1")
    with pytest.raises(ValueError, match="group_id"):
        sparkplug_topic("fact/ory", "DDATA", "edge1")
    with pytest.raises(ValueError, match="edge_node_id"):
        sparkplug_topic("factory", "DDATA", "edge#1")


def test_sequence_numbers_wrap_at_255():
    """A consumer detects a missed message from a gap in seq."""
    assert next_seq(0) == 1
    assert next_seq(254) == 255
    assert next_seq(255) == 0


def test_birth_payload_carries_every_metric_with_an_alias(db):
    """DBIRTH is what lets DDATA send aliases instead of names."""
    publisher = SparkplugPublisher(SparkplugConfig())
    tags = db.tags()[:5]
    payload = json.loads(publisher.birth_payload(tags, 1000.0))
    assert len(payload["metrics"]) == 5
    assert payload["timestamp"] == 1_000_000
    assert {m["name"] for m in payload["metrics"]} == {t.name for t in tags}
    assert sorted(m["alias"] for m in payload["metrics"]) == [1, 2, 3, 4, 5]
    assert payload["metrics"][0]["properties"]["engUnit"] == tags[0].unit


def test_report_by_exception_only_publishes_changes(db):
    """After the birth, an unchanged metric must not be republished."""
    publisher = SparkplugPublisher(SparkplugConfig())
    publisher.assign_aliases(db.tags()[:2])
    readings = {
        "conveyor_speed": Reading("conveyor_speed", 30.0, 1.0, Quality.GOOD),
        "conveyor_speed_sp": Reading("conveyor_speed_sp", 30.0, 1.0, Quality.GOOD),
    }
    assert len(publisher.changed_metrics(readings)) == 2
    assert publisher.changed_metrics(readings) == []
    readings["conveyor_speed"] = Reading("conveyor_speed", 31.0, 2.0, Quality.GOOD)
    changed = publisher.changed_metrics(readings)
    assert len(changed) == 1
    assert changed[0]["alias"] == 1


def test_report_by_exception_honours_a_deadband(db):
    """The same deadband idea as the historian, applied to the wire."""
    publisher = SparkplugPublisher(SparkplugConfig())
    publisher.assign_aliases([db["conveyor_speed"]])
    first = {"conveyor_speed": Reading("conveyor_speed", 30.0, 1.0, Quality.GOOD)}
    publisher.changed_metrics(first)
    small = {"conveyor_speed": Reading("conveyor_speed", 30.05, 2.0, Quality.GOOD)}
    assert publisher.changed_metrics(small, {"conveyor_speed": 0.2}) == []
    big = {"conveyor_speed": Reading("conveyor_speed", 31.0, 3.0, Quality.GOOD)}
    assert len(publisher.changed_metrics(big, {"conveyor_speed": 0.2})) == 1


def test_bad_quality_readings_are_not_published(db):
    """Publishing a BAD reading as a value is how stale data spreads."""
    publisher = SparkplugPublisher(SparkplugConfig())
    readings = {"x": Reading("x", None, 1.0, Quality.BAD, error="offline")}
    assert publisher.changed_metrics(readings) == []


def test_death_payload_carries_the_birth_sequence():
    """NDEATH is registered as the MQTT will so the broker sends it for you."""
    publisher = SparkplugPublisher(SparkplugConfig())
    payload = json.loads(publisher.death_payload(1000.0))
    assert payload["metrics"][0]["name"] == "bdSeq"
    assert payload["timestamp"] == 1_000_000


def test_publisher_topics_use_its_own_identity():
    """Group, edge node and device come from the config, not the call site."""
    publisher = SparkplugPublisher(SparkplugConfig(group_id="g", edge_node_id="e", device_id="d"))
    assert publisher.topic("DDATA") == "spBv1.0/g/DDATA/e/d"
    assert publisher.topic("NBIRTH", include_device=False) == "spBv1.0/g/NBIRTH/e"


def test_publisher_is_output_only(db):
    """A Sparkplug edge node publishes; it is not polled."""
    publisher = SparkplugPublisher(SparkplugConfig())
    with pytest.raises(NotImplementedError, match="output driver"):
        publisher.read(db.tags())


def test_publish_without_a_broker_still_records_what_it_would_send(db, clock):
    """The payload path is testable offline; only the socket is optional."""
    publisher = SparkplugPublisher(SparkplugConfig(), clock=clock)
    publisher.publish_birth(db.tags()[:3])
    assert [topic for topic, _ in publisher.published] == [
        "spBv1.0/factory/NBIRTH/factorylink-edge",
        "spBv1.0/factory/DBIRTH/factorylink-edge/line1",
    ]
    sent = publisher.publish_data(
        {"conveyor_speed": Reading("conveyor_speed", 30.0, 1.0, Quality.GOOD)}
    )
    assert sent == 1
    assert publisher.published[-1][0].endswith("/DDATA/factorylink-edge/line1")
    assert publisher.seq == 3


def test_driver_is_abstract():
    """The ABC must refuse to be instantiated directly."""
    with pytest.raises(TypeError):
        Driver()  # type: ignore[abstract]


def test_quality_usable_is_only_true_for_good():
    """STALE is not usable, and neither is BAD."""
    assert Quality.GOOD.usable is True
    assert Quality.BAD.usable is False
    assert Quality.STALE.usable is False
