"""MQTT Sparkplug B publisher, with the paho-mqtt import guarded.

Sparkplug B is the convention that makes MQTT usable for plant data. The parts
that matter, and that this module implements:

* **Topic namespace.** ``spBv1.0/<group>/<message type>/<edge node>/<device>``.
  Message types used here: ``NBIRTH``/``NDEATH`` for the edge node,
  ``DBIRTH``/``DDEATH`` for a device, and ``DDATA`` for changes.
* **Birth and death certificates.** The client registers the NDEATH payload as
  the MQTT *will* at connect time, so if the edge node drops off the network
  the broker publishes the death certificate on its behalf. Without this, a
  consumer cannot tell "nothing changed" from "the gateway is gone" -- which
  is the same stale-data problem that quality flags solve inside this package.
* **Report by exception.** After the birth, only changed metrics are
  published. The birth carries every metric with its alias, so DDATA can carry
  the alias instead of the name.
* **Monotonic sequence numbers.** ``seq`` increments 0..255 and wraps; a
  consumer that sees a gap knows it missed a message.

Payload encoding here is JSON rather than protobuf, so the module has no
mandatory dependency and the topic/sequence logic stays unit-testable. Swap
:meth:`SparkplugPublisher.encode_payload` for a protobuf encoder if the
consumer requires the binary form.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..clock import Clock, SystemClock
from ..tags import TagDef
from .base import ConnectionError_, Driver, Quality, Reading, require

try:  # pragma: no cover - depends on the environment
    import paho.mqtt.client as _mqtt  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001
    _mqtt = None  # type: ignore[assignment]

__all__ = [
    "SparkplugConfig",
    "SparkplugPublisher",
    "MQTT_AVAILABLE",
    "sparkplug_topic",
    "next_seq",
]

MQTT_AVAILABLE = _mqtt is not None

SPARKPLUG_NAMESPACE = "spBv1.0"


def sparkplug_topic(
    group_id: str, message_type: str, edge_node_id: str, device_id: str | None = None
) -> str:
    """Build a Sparkplug B topic.

    Pure function so the namespace rules can be tested without a broker.
    """
    valid = {"NBIRTH", "NDEATH", "NDATA", "NCMD", "DBIRTH", "DDEATH", "DDATA", "DCMD", "STATE"}
    if message_type not in valid:
        raise ValueError(f"unknown Sparkplug message type {message_type!r}")
    for part, label in ((group_id, "group_id"), (edge_node_id, "edge_node_id")):
        if not part or "/" in part or "+" in part or "#" in part:
            raise ValueError(f"{label} {part!r} is not a legal Sparkplug topic element")
    parts = [SPARKPLUG_NAMESPACE, group_id, message_type, edge_node_id]
    if device_id:
        parts.append(device_id)
    return "/".join(parts)


def next_seq(seq: int) -> int:
    """Increment a Sparkplug sequence number, wrapping 255 -> 0."""
    return (int(seq) + 1) % 256


@dataclass
class SparkplugConfig:
    """Broker and namespace settings."""

    host: str = "127.0.0.1"
    port: int = 1883
    group_id: str = "factory"
    edge_node_id: str = "factorylink-edge"
    device_id: str = "line1"
    username: str | None = None
    password: str | None = None
    keepalive: int = 30
    qos: int = 0
    client_id: str = "factorylink"
    extra: dict[str, Any] = field(default_factory=dict)


class SparkplugPublisher(Driver):
    """Publish tag readings to MQTT as Sparkplug B messages.

    This is an output-only driver: :meth:`read` raises, because a Sparkplug
    edge node publishes rather than being polled. It still implements the
    :class:`~factorylink.protocols.base.Driver` interface so the same
    connection health and statistics handling applies.
    """

    protocol = "mqtt-sparkplug"

    def __init__(
        self,
        config: SparkplugConfig | None = None,
        clock: Clock | None = None,
        device: str | None = None,
    ) -> None:
        self.config = config or SparkplugConfig()
        super().__init__(device=device or self.config.device_id)
        self.clock = clock or SystemClock()
        self._client: Any = None
        self.seq = 0
        self.bd_seq = 0
        self.aliases: dict[str, int] = {}
        self._last_published: dict[str, float | bool] = {}
        self.published: list[tuple[str, str]] = []

    # -- payload construction (pure, testable without a broker) -----------

    def assign_aliases(self, tags: Sequence[TagDef]) -> dict[str, int]:
        """Give every tag a stable numeric alias, starting at 1."""
        self.aliases = {tag.name: index + 1 for index, tag in enumerate(tags)}
        return dict(self.aliases)

    def encode_payload(self, metrics: Sequence[Mapping[str, Any]], timestamp: float) -> bytes:
        """Encode a Sparkplug payload. JSON here; swap for protobuf if needed."""
        body = {
            "timestamp": int(timestamp * 1000),
            "seq": self.seq,
            "metrics": list(metrics),
        }
        return json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def birth_payload(self, tags: Sequence[TagDef], timestamp: float) -> bytes:
        """DBIRTH payload: every metric, with name, alias, type and unit."""
        self.assign_aliases(tags)
        metrics = [
            {
                "name": tag.name,
                "alias": self.aliases[tag.name],
                "dataType": tag.data_type.value,
                "properties": {"engUnit": tag.unit, "description": tag.description},
                "value": None,
            }
            for tag in tags
        ]
        return self.encode_payload(metrics, timestamp)

    def death_payload(self, timestamp: float) -> bytes:
        """NDEATH payload, registered as the MQTT will."""
        return json.dumps(
            {"timestamp": int(timestamp * 1000), "metrics": [{"name": "bdSeq", "value": self.bd_seq}]},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def changed_metrics(
        self, readings: Mapping[str, Reading], deadbands: Mapping[str, float] | None = None
    ) -> list[dict[str, Any]]:
        """Report by exception: only metrics that moved beyond their deadband."""
        bands = deadbands or {}
        out: list[dict[str, Any]] = []
        for name, reading in readings.items():
            if reading.quality is not Quality.GOOD or reading.value is None:
                continue
            previous = self._last_published.get(name)
            band = float(bands.get(name, 0.0))
            if previous is not None and not isinstance(reading.value, bool):
                if abs(float(reading.value) - float(previous)) <= band:
                    continue
            elif previous is not None and previous == reading.value:
                continue
            self._last_published[name] = reading.value
            metric: dict[str, Any] = {"value": reading.value, "timestamp": int(reading.timestamp * 1000)}
            if name in self.aliases:
                metric["alias"] = self.aliases[name]
            else:
                metric["name"] = name
            out.append(metric)
        return out

    def topic(self, message_type: str, include_device: bool = True) -> str:
        """Topic for this publisher's group/edge node/device."""
        cfg = self.config
        return sparkplug_topic(
            cfg.group_id,
            message_type,
            cfg.edge_node_id,
            cfg.device_id if include_device else None,
        )

    # -- transport --------------------------------------------------------

    def connect(self) -> None:
        """Connect to the broker and register the death certificate as the will."""
        if self._connected:
            return
        require(_mqtt, "SparkplugPublisher", "paho-mqtt")
        cfg = self.config
        client = _mqtt.Client(client_id=cfg.client_id)  # type: ignore[union-attr]
        if cfg.username:
            client.username_pw_set(cfg.username, cfg.password or "")
        self.bd_seq = next_seq(self.bd_seq)
        client.will_set(
            self.topic("NDEATH", include_device=False),
            self.death_payload(self.clock.now()),
            qos=cfg.qos,
            retain=False,
        )
        try:
            client.connect(cfg.host, cfg.port, cfg.keepalive)
        except Exception as exc:  # noqa: BLE001
            raise ConnectionError_(f"{self.device}: cannot reach broker {cfg.host}: {exc}") from exc
        client.loop_start()
        self._client = client
        self._connected = True
        self.stats.connects += 1

    def disconnect(self) -> None:
        """Publish a device death certificate and close the connection."""
        if self._client is not None:
            try:
                self._publish(self.topic("DDEATH"), self.death_payload(self.clock.now()))
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass
        self._client = None
        if self._connected:
            self.stats.disconnects += 1
        self._connected = False

    def _publish(self, topic: str, payload: bytes) -> None:
        self.published.append((topic, payload.decode("utf-8")))
        if self._client is not None:
            self._client.publish(topic, payload, qos=self.config.qos)
        self.seq = next_seq(self.seq)

    def publish_birth(self, tags: Sequence[TagDef]) -> None:
        """Send NBIRTH then DBIRTH, resetting the sequence number to 0."""
        self.seq = 0
        now = self.clock.now()
        self._publish(self.topic("NBIRTH", include_device=False), self.encode_payload([], now))
        self._publish(self.topic("DBIRTH"), self.birth_payload(tags, now))

    def publish_data(
        self, readings: Mapping[str, Reading], deadbands: Mapping[str, float] | None = None
    ) -> int:
        """Publish changed metrics as DDATA. Returns the metric count sent."""
        metrics = self.changed_metrics(readings, deadbands)
        if not metrics:
            return 0
        self._publish(self.topic("DDATA"), self.encode_payload(metrics, self.clock.now()))
        return len(metrics)

    # -- Driver contract --------------------------------------------------

    def read(self, tags: Sequence[TagDef]) -> dict[str, Reading]:
        """Not supported: a Sparkplug edge node publishes, it is not polled."""
        raise NotImplementedError(
            "SparkplugPublisher is an output driver; poll a Modbus or OPC UA "
            "driver and pass the readings to publish_data()"
        )

    def write(self, tag: TagDef, value: float | bool) -> None:
        """Publish a single metric change immediately as DDATA."""
        reading = Reading(tag.name, value, self.clock.now(), Quality.GOOD)
        self.stats.writes += 1
        self.publish_data({tag.name: reading})
