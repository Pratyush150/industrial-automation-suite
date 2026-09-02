"""Write-path protection.

READ THIS FIRST
---------------
**This package is a data-acquisition and monitoring tool. It is not a safety
instrumented system and must never be used as one.**

Nothing here is rated, certified, or designed to any functional-safety
standard. There is no SIL rating, no redundancy, no proof-test interval, no
fail-safe behaviour on loss of power or loss of network. It runs on a general
purpose operating system over a general purpose network, both of which can and
will stall for seconds at a time.

Safety functions belong in a safety PLC or hardwired safety relay, with an
E-stop circuit that does not depend on software running on a PC. Interlocks
belong in the control PLC. This layer exists to reduce the chance that a
monitoring tool does something stupid to a live process -- not to be relied on
to prevent harm.

With that said, a data-acquisition tool that can write is a real hazard, and
the mitigations here are the ones worth having:

* **Read-only by default.** Writes are refused until someone deliberately
  turns them on. The default is not a suggestion; it is the setting.
* **Allow-list.** Only tags explicitly listed as writable, in both the tag
  database and the policy, can be written. There is no wildcard.
* **Range clamping.** A value outside the tag's engineering range is clamped
  (or rejected, if configured), so a units mistake becomes a clipped setpoint
  rather than a full-scale command.
* **Rate limiting.** A bounded number of writes per tag per window, so a stuck
  loop in calling code cannot hammer a setpoint.
* **Confirmation tokens.** Critical tags need a two-step write: request a
  token for a specific tag and value, then present it. A token is single-use,
  bound to the value, and expires.
* **Audit log.** Every attempt, accepted or refused, with who and why.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .clock import Clock, SystemClock
from .tags import TagDatabase, TagDef

__all__ = [
    "SafetyError",
    "ReadOnlyModeError",
    "NotAllowListedError",
    "NotWritableError",
    "OutOfRangeError",
    "RateLimitedError",
    "ConfirmationRequiredError",
    "InvalidTokenError",
    "WritePolicy",
    "WriteRequest",
    "ApprovedWrite",
    "AuditEntry",
    "WriteGuard",
    "SAFETY_NOTICE",
]

SAFETY_NOTICE = (
    "factorylink is a monitoring and data-acquisition tool. It is NOT a safety "
    "instrumented system and must not be used to implement a safety function, "
    "an interlock, or an emergency stop. Those belong in a safety PLC or a "
    "hardwired safety circuit."
)


class SafetyError(Exception):
    """Base class for a refused write."""


class ReadOnlyModeError(SafetyError):
    """Writes are globally disabled."""


class NotAllowListedError(SafetyError):
    """The tag is not on the policy allow-list."""


class NotWritableError(SafetyError):
    """The tag database does not mark the tag as writable."""


class OutOfRangeError(SafetyError):
    """The value is outside the tag range and clamping is disabled."""


class RateLimitedError(SafetyError):
    """Too many writes to this tag in the rate-limit window."""


class ConfirmationRequiredError(SafetyError):
    """A critical tag was written without a confirmation token."""


class InvalidTokenError(SafetyError):
    """The confirmation token was wrong, expired, reused or for another value."""


@dataclass
class WritePolicy:
    """What the write path is permitted to do.

    Defaults are deliberately restrictive: read-only, empty allow-list.
    """

    #: Master switch. Nothing is written while this is True.
    read_only: bool = True
    #: Tags that may be written at all. Empty means nothing may be written.
    allow_list: frozenset[str] = frozenset()
    #: Tags that additionally require a confirmation token.
    require_confirmation: frozenset[str] = frozenset()
    #: Clamp out-of-range values instead of refusing them.
    clamp_out_of_range: bool = True
    #: Maximum writes per tag inside ``rate_window`` seconds.
    max_writes_per_window: int = 6
    rate_window: float = 60.0
    #: Confirmation token lifetime, seconds.
    token_ttl: float = 60.0
    operator: str = "operator"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "WritePolicy":
        """Build a policy from a config mapping."""
        return cls(
            read_only=bool(data.get("read_only", True)),
            allow_list=frozenset(data.get("allow_list", []) or []),
            require_confirmation=frozenset(data.get("require_confirmation", []) or []),
            clamp_out_of_range=bool(data.get("clamp_out_of_range", True)),
            max_writes_per_window=int(data.get("max_writes_per_window", 6)),
            rate_window=float(data.get("rate_window", 60.0)),
            token_ttl=float(data.get("token_ttl", 60.0)),
            operator=str(data.get("operator", "operator")),
        )

    def allowing(self, tags: Iterable[str]) -> "WritePolicy":
        """Return a copy with writes enabled for ``tags``.

        Making "turn writes on" an explicit, named call keeps it out of the
        default path and greppable in a code review.
        """
        return WritePolicy(
            read_only=False,
            allow_list=frozenset(tags),
            require_confirmation=self.require_confirmation,
            clamp_out_of_range=self.clamp_out_of_range,
            max_writes_per_window=self.max_writes_per_window,
            rate_window=self.rate_window,
            token_ttl=self.token_ttl,
            operator=self.operator,
        )


@dataclass(frozen=True)
class WriteRequest:
    """A requested write, before any checks."""

    tag: str
    value: float | bool
    operator: str = "operator"
    token: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class ApprovedWrite:
    """A write that passed every check, with the value that will be sent."""

    tag: TagDef
    value: float | bool
    requested: float | bool
    clamped: bool
    operator: str
    timestamp: float

    @property
    def raw(self) -> float | bool:
        """The raw register value that will go on the wire."""
        return self.tag.to_raw(self.value)


@dataclass
class AuditEntry:
    """One line of the write audit trail."""

    timestamp: float
    tag: str
    requested: float | bool
    applied: float | bool | None
    operator: str
    accepted: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form."""
        return {
            "timestamp": self.timestamp,
            "tag": self.tag,
            "requested": self.requested,
            "applied": self.applied,
            "operator": self.operator,
            "accepted": self.accepted,
            "detail": self.detail,
        }


@dataclass
class _Token:
    tag: str
    value: float | bool
    digest: str
    expires_at: float
    operator: str
    used: bool = False


class WriteGuard:
    """Enforce :class:`WritePolicy` on every write.

    Typical use::

        guard = WriteGuard(db, WritePolicy().allowing({"conveyor_speed_sp"}), clock)
        approved = guard.check("conveyor_speed_sp", 28.0, operator="alice")
        driver.write(approved.tag, approved.value)

    or, letting the guard do the driver call and the audit entry in one step::

        guard.apply(driver, "conveyor_speed_sp", 28.0, operator="alice")
    """

    def __init__(
        self,
        db: TagDatabase,
        policy: WritePolicy | None = None,
        clock: Clock | None = None,
        audit_limit: int = 500,
    ) -> None:
        self.db = db
        self.policy = policy or WritePolicy()
        self.clock = clock or SystemClock()
        self.audit: deque[AuditEntry] = deque(maxlen=audit_limit)
        self._secret = secrets.token_bytes(32)
        self._tokens: dict[str, _Token] = {}
        self._history: dict[str, deque[float]] = {}

    # -- confirmation tokens ------------------------------------------------

    def issue_token(self, tag_name: str, value: float | bool, operator: str = "operator") -> str:
        """Issue a single-use token bound to one tag and one value."""
        tag = self.db[tag_name]
        now = self.clock.now()
        nonce = secrets.token_hex(8)
        digest = self._digest(tag.name, value, nonce)
        token = f"{nonce}.{digest[:16]}"
        self._tokens[token] = _Token(
            tag=tag.name,
            value=value,
            digest=digest,
            expires_at=now + self.policy.token_ttl,
            operator=operator,
        )
        return token

    def _digest(self, tag: str, value: float | bool, nonce: str) -> str:
        payload = f"{tag}|{value!r}|{nonce}".encode("utf-8")
        return hmac.new(self._secret, payload, hashlib.sha256).hexdigest()

    def _consume_token(self, token: str | None, tag: str, value: float | bool, now: float) -> None:
        if not token:
            raise ConfirmationRequiredError(
                f"{tag} is a critical tag: request a confirmation token for this "
                f"exact value with issue_token() and present it with the write"
            )
        record = self._tokens.get(token)
        if record is None or record.used:
            raise InvalidTokenError(f"confirmation token for {tag} is unknown or already used")
        if now > record.expires_at:
            del self._tokens[token]
            raise InvalidTokenError(
                f"confirmation token for {tag} expired "
                f"{now - record.expires_at:.1f}s ago; request a new one"
            )
        if record.tag != tag:
            raise InvalidTokenError(f"token was issued for {record.tag}, not {tag}")
        nonce = token.split(".", 1)[0]
        if not hmac.compare_digest(record.digest, self._digest(tag, value, nonce)):
            raise InvalidTokenError(
                f"token for {tag} was issued for value {record.value!r}, not {value!r}"
            )
        record.used = True
        del self._tokens[token]

    # -- rate limiting ------------------------------------------------------

    def _check_rate(self, tag: str, now: float) -> None:
        window = self._history.setdefault(tag, deque(maxlen=256))
        horizon = now - self.policy.rate_window
        while window and window[0] < horizon:
            window.popleft()
        if len(window) >= self.policy.max_writes_per_window:
            raise RateLimitedError(
                f"{tag}: {len(window)} writes in the last {self.policy.rate_window:g}s, "
                f"limit is {self.policy.max_writes_per_window}"
            )

    def _record_rate(self, tag: str, now: float) -> None:
        self._history.setdefault(tag, deque(maxlen=256)).append(now)

    # -- the check ----------------------------------------------------------

    def check(
        self,
        tag_name: str,
        value: float | bool,
        operator: str | None = None,
        token: str | None = None,
        reason: str = "",
    ) -> ApprovedWrite:
        """Validate a write. Returns the approved write or raises.

        The order of checks matters: mode, then allow-list, then the tag's own
        writable flag, then confirmation, then rate, then range. Cheapest and
        most categorical first, so a refusal message names the real reason.
        """
        now = self.clock.now()
        who = operator or self.policy.operator
        try:
            approved = self._check(tag_name, value, who, token, now)
        except SafetyError as exc:
            self.audit.append(
                AuditEntry(now, tag_name, value, None, who, False, f"{type(exc).__name__}: {exc}")
            )
            raise
        detail = reason or ("clamped" if approved.clamped else "")
        self.audit.append(
            AuditEntry(now, tag_name, value, approved.value, who, True, detail)
        )
        self._record_rate(tag_name, now)
        return approved

    def _check(
        self, tag_name: str, value: float | bool, operator: str, token: str | None, now: float
    ) -> ApprovedWrite:
        if self.policy.read_only:
            raise ReadOnlyModeError(
                f"refusing to write {tag_name}: this instance is in read-only mode. "
                f"Writes require an explicit WritePolicy with read_only=False."
            )
        if tag_name not in self.policy.allow_list:
            raise NotAllowListedError(
                f"refusing to write {tag_name}: it is not on the write allow-list "
                f"({len(self.policy.allow_list)} tag(s) allowed)"
            )
        tag = self.db[tag_name]
        if not tag.writable:
            raise NotWritableError(
                f"refusing to write {tag_name}: the tag database marks it read-only"
            )
        if tag_name in self.policy.require_confirmation:
            self._consume_token(token, tag_name, value, now)
        self._check_rate(tag_name, now)

        if isinstance(value, bool) or tag.is_bit:
            return ApprovedWrite(tag, bool(value), value, False, operator, now)

        numeric = float(value)
        if not tag.in_range(numeric):
            if not self.policy.clamp_out_of_range:
                raise OutOfRangeError(
                    f"refusing to write {numeric:g} to {tag_name}: outside the "
                    f"configured range [{tag.min_value}, {tag.max_value}]"
                )
            return ApprovedWrite(tag, tag.clamp(numeric), numeric, True, operator, now)
        return ApprovedWrite(tag, numeric, numeric, False, operator, now)

    # -- convenience --------------------------------------------------------

    def apply(
        self,
        driver: Any,
        tag_name: str,
        value: float | bool,
        operator: str | None = None,
        token: str | None = None,
        reason: str = "",
    ) -> ApprovedWrite:
        """Check the write, then perform it through ``driver``."""
        approved = self.check(tag_name, value, operator, token, reason)
        driver.write(approved.tag, approved.value)
        return approved

    def writable_tags(self) -> list[str]:
        """Tags that would currently pass the allow-list and writable checks."""
        if self.policy.read_only:
            return []
        return sorted(
            name
            for name in self.policy.allow_list
            if (tag := self.db.get(name)) is not None and tag.writable
        )

    def audit_log(self) -> list[dict[str, Any]]:
        """The audit trail, oldest first."""
        return [entry.as_dict() for entry in self.audit]

    def status(self) -> dict[str, Any]:
        """Current policy state, shown in the dashboard footer."""
        return {
            "read_only": self.policy.read_only,
            "allow_list": sorted(self.policy.allow_list),
            "require_confirmation": sorted(self.policy.require_confirmation),
            "clamp_out_of_range": self.policy.clamp_out_of_range,
            "max_writes_per_window": self.policy.max_writes_per_window,
            "rate_window": self.policy.rate_window,
            "writes_accepted": sum(1 for e in self.audit if e.accepted),
            "writes_refused": sum(1 for e in self.audit if not e.accepted),
            "notice": SAFETY_NOTICE,
        }
