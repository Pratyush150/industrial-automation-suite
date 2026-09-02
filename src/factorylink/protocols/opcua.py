"""OPC UA driver, with the asyncua import guarded.

OPC UA replaces "register 40021, two words, little word order" with a browsable
address space and real data types, which removes an entire class of bug. It
introduces different ones: certificate trust, session timeouts, and the fact
that a subscription publishing at 100 ms will happily flatten a small server.

In this package a tag's ``address`` field is reinterpreted for OPC UA: the
node id lives in ``description`` when it is not a plain numeric identifier.
Use :func:`node_id_for` to see exactly what a tag maps to.

The driver is synchronous on the outside and drives asyncua's event loop
internally, because the poller is a synchronous scan loop. That is a
deliberate trade: one loop, one place where timing is reasoned about.
"""

from __future__ import annotations

import asyncio
from typing import Any, Sequence

from ..tags import TagDef
from .base import ConnectionError_, Driver, ProtocolError, Quality, Reading, require

try:  # pragma: no cover - depends on the environment
    from asyncua import Client as _AsyncuaClient  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001
    _AsyncuaClient = None  # type: ignore[assignment]

__all__ = ["OpcUaDriver", "ASYNCUA_AVAILABLE", "node_id_for"]

ASYNCUA_AVAILABLE = _AsyncuaClient is not None


def node_id_for(tag: TagDef, namespace: int = 2) -> str:
    """Return the OPC UA node id a tag maps to.

    Two conventions are supported, in order:

    1. ``description`` starting with ``ns=`` is used verbatim. This is the
       escape hatch for servers with string identifiers containing anything.
    2. Otherwise the tag name becomes a string identifier in ``namespace``.

    Pure function, so the mapping is unit-testable without a server.
    """
    hint = (tag.description or "").strip()
    if hint.startswith("ns="):
        return hint.split()[0]
    return f"ns={namespace};s={tag.name}"


class OpcUaDriver(Driver):
    """Read and write OPC UA nodes using the same Driver contract."""

    protocol = "opc-ua"

    def __init__(
        self,
        url: str = "opc.tcp://127.0.0.1:4840/freeopcua/server/",
        device: str = "opcua1",
        namespace: int = 2,
        timeout: float = 4.0,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        super().__init__(device=device)
        require(_AsyncuaClient, "OpcUaDriver", "asyncua")
        self.url = url
        self.namespace = namespace
        self.timeout = timeout
        self.username = username
        self.password = password
        self._client: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- lifecycle --------------------------------------------------------

    def _run(self, coro: Any) -> Any:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    def connect(self) -> None:
        """Open a session with the server."""
        if self._connected:
            return
        client = _AsyncuaClient(url=self.url, timeout=self.timeout)  # type: ignore[misc]
        if self.username:
            client.set_user(self.username)
        if self.password:
            client.set_password(self.password)
        try:
            self._run(client.connect())
        except Exception as exc:  # noqa: BLE001
            raise ConnectionError_(f"{self.device}: cannot connect to {self.url}: {exc}") from exc
        self._client = client
        self._connected = True
        self.stats.connects += 1

    def disconnect(self) -> None:
        """Close the session. Safe to call twice."""
        if self._client is not None:
            try:
                self._run(self._client.disconnect())
            except Exception:  # noqa: BLE001 - closing must never raise
                pass
        self._client = None
        if self._loop is not None:
            self._loop.close()
            self._loop = None
        if self._connected:
            self.stats.disconnects += 1
        self._connected = False

    # -- data -------------------------------------------------------------

    def read(self, tags: Sequence[TagDef]) -> dict[str, Reading]:
        """Read a batch of nodes in a single service call where possible."""
        if not self._connected or self._client is None:
            raise ConnectionError_(f"{self.device}: not connected")
        now = self._now()
        try:
            values = self._run(self._read_async(tags))
        except Exception as exc:  # noqa: BLE001 - degraded, not fatal
            self.stats.read_failures += 1
            self.stats.last_error = str(exc)
            return self.bad_readings(tags, str(exc), now)
        self.stats.reads += 1
        out: dict[str, Reading] = {}
        for tag, value in zip(tags, values):
            if value is None:
                out[tag.name] = Reading(tag.name, None, now, Quality.BAD, error="null value")
                continue
            converted = bool(value) if tag.is_bit else tag.to_engineering(float(value))
            out[tag.name] = Reading(tag.name, converted, now, Quality.GOOD)
        return out

    async def _read_async(self, tags: Sequence[TagDef]) -> list[Any]:
        nodes = [self._client.get_node(node_id_for(tag, self.namespace)) for tag in tags]
        return list(await self._client.read_values(nodes))

    def write(self, tag: TagDef, value: float | bool) -> None:
        """Write one node value. Policy checks belong upstream."""
        if not self._connected or self._client is None:
            raise ConnectionError_(f"{self.device}: not connected")
        self.stats.writes += 1
        payload: Any = bool(value) if tag.is_bit else tag.to_raw(value)
        try:
            node = self._client.get_node(node_id_for(tag, self.namespace))
            self._run(node.write_value(payload))
        except Exception as exc:  # noqa: BLE001
            self.stats.write_failures += 1
            self.stats.last_error = str(exc)
            raise ProtocolError(f"write to {tag.name} failed: {exc}") from exc
