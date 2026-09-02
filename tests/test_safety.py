"""Write-path protection: allow-list, clamping, rate limit, confirmation."""

from __future__ import annotations

import pytest

from factorylink.safety import (
    ConfirmationRequiredError,
    InvalidTokenError,
    NotAllowListedError,
    NotWritableError,
    OutOfRangeError,
    RateLimitedError,
    ReadOnlyModeError,
    SAFETY_NOTICE,
    WriteGuard,
    WritePolicy,
)


@pytest.fixture()
def guard(db, clock) -> WriteGuard:
    """A guard with writes enabled for exactly one tag."""
    policy = WritePolicy().allowing({"conveyor_speed_sp"})
    return WriteGuard(db, policy, clock)


def test_read_only_is_the_default(db, clock):
    """Nothing is written until somebody deliberately turns writes on."""
    default = WriteGuard(db, WritePolicy(), clock)
    assert default.policy.read_only is True
    assert default.policy.allow_list == frozenset()
    with pytest.raises(ReadOnlyModeError, match="read-only mode"):
        default.check("conveyor_speed_sp", 25.0)
    assert default.writable_tags() == []


def test_a_write_to_a_non_allow_listed_tag_is_rejected(guard):
    """The allow-list is a list, not a pattern. tank_level is not on it."""
    with pytest.raises(NotAllowListedError, match="allow-list"):
        guard.check("tank_level", 50.0)
    with pytest.raises(NotAllowListedError):
        guard.check("target_rate", 100)


def test_allow_listing_a_tag_the_database_says_is_read_only_still_fails(db, clock):
    """Two independent gates: policy allow-list and the tag's writable flag."""
    policy = WritePolicy().allowing({"tank_level"})
    guard = WriteGuard(db, policy, clock)
    with pytest.raises(NotWritableError, match="marks it read-only"):
        guard.check("tank_level", 50.0)


def test_an_out_of_range_value_is_clamped(guard):
    """A units mistake becomes a clipped setpoint, not a full-scale command."""
    approved = guard.check("conveyor_speed_sp", 999.0)
    assert approved.clamped is True
    assert approved.requested == 999.0
    assert approved.value == 45.0
    assert approved.tag.max_value == 45.0


def test_a_negative_value_is_clamped_to_the_minimum(guard):
    """Clamping works in both directions."""
    approved = guard.check("conveyor_speed_sp", -20.0)
    assert approved.value == 0.0
    assert approved.clamped is True


def test_an_in_range_value_passes_untouched(guard):
    """The happy path must not be surprising."""
    approved = guard.check("conveyor_speed_sp", 28.5)
    assert approved.value == 28.5
    assert approved.clamped is False
    assert approved.raw == pytest.approx(28.5)


def test_clamping_can_be_turned_into_a_refusal(db, clock):
    """Some sites would rather the write fail loudly."""
    policy = WritePolicy().allowing({"conveyor_speed_sp"})
    policy.clamp_out_of_range = False
    guard = WriteGuard(db, policy, clock)
    with pytest.raises(OutOfRangeError, match="outside the"):
        guard.check("conveyor_speed_sp", 999.0)


def test_rate_limiting_stops_a_runaway_caller(guard, clock):
    """A stuck loop must not hammer a setpoint."""
    for _ in range(guard.policy.max_writes_per_window):
        guard.check("conveyor_speed_sp", 25.0)
    with pytest.raises(RateLimitedError, match="writes in the last"):
        guard.check("conveyor_speed_sp", 25.0)


def test_the_rate_limit_window_slides(guard, clock):
    """After the window passes, writes are allowed again."""
    for _ in range(guard.policy.max_writes_per_window):
        guard.check("conveyor_speed_sp", 25.0)
    clock.advance(guard.policy.rate_window + 1.0)
    approved = guard.check("conveyor_speed_sp", 25.0)
    assert approved.value == 25.0


def test_a_critical_tag_needs_a_confirmation_token(db, clock):
    """Two-step write for the tags where a mistake matters."""
    policy = WritePolicy().allowing({"conveyor_speed_sp"})
    policy.require_confirmation = frozenset({"conveyor_speed_sp"})
    guard = WriteGuard(db, policy, clock)
    with pytest.raises(ConfirmationRequiredError, match="confirmation token"):
        guard.check("conveyor_speed_sp", 25.0)
    token = guard.issue_token("conveyor_speed_sp", 25.0)
    approved = guard.check("conveyor_speed_sp", 25.0, token=token)
    assert approved.value == 25.0


def test_a_confirmation_token_is_single_use(db, clock):
    """Replaying a token would defeat the point of confirming."""
    policy = WritePolicy().allowing({"conveyor_speed_sp"})
    policy.require_confirmation = frozenset({"conveyor_speed_sp"})
    guard = WriteGuard(db, policy, clock)
    token = guard.issue_token("conveyor_speed_sp", 25.0)
    guard.check("conveyor_speed_sp", 25.0, token=token)
    with pytest.raises(InvalidTokenError, match="already used"):
        guard.check("conveyor_speed_sp", 25.0, token=token)


def test_a_token_is_bound_to_the_value_it_confirmed(db, clock):
    """Confirming 25 must not authorise writing 45."""
    policy = WritePolicy().allowing({"conveyor_speed_sp"})
    policy.require_confirmation = frozenset({"conveyor_speed_sp"})
    guard = WriteGuard(db, policy, clock)
    token = guard.issue_token("conveyor_speed_sp", 25.0)
    with pytest.raises(InvalidTokenError, match="issued for value"):
        guard.check("conveyor_speed_sp", 45.0, token=token)


def test_a_token_expires(db, clock):
    """A confirmation from an hour ago is not a confirmation."""
    policy = WritePolicy().allowing({"conveyor_speed_sp"})
    policy.require_confirmation = frozenset({"conveyor_speed_sp"})
    policy.token_ttl = 30.0
    guard = WriteGuard(db, policy, clock)
    token = guard.issue_token("conveyor_speed_sp", 25.0)
    clock.advance(31.0)
    with pytest.raises(InvalidTokenError, match="expired"):
        guard.check("conveyor_speed_sp", 25.0, token=token)


def test_an_unknown_token_is_rejected(db, clock):
    """A guessed token is not a token."""
    policy = WritePolicy().allowing({"conveyor_speed_sp"})
    policy.require_confirmation = frozenset({"conveyor_speed_sp"})
    guard = WriteGuard(db, policy, clock)
    with pytest.raises(InvalidTokenError, match="unknown"):
        guard.check("conveyor_speed_sp", 25.0, token="deadbeef.0000")


def test_boolean_writes_go_through_unchanged(db, clock):
    """Coil commands are booleans, not clamped numbers."""
    policy = WritePolicy().allowing({"cmd_start"})
    guard = WriteGuard(db, policy, clock)
    approved = guard.check("cmd_start", True)
    assert approved.value is True


def test_every_attempt_is_audited(guard):
    """Refusals matter more than successes in an audit trail."""
    guard.check("conveyor_speed_sp", 25.0)
    with pytest.raises(NotAllowListedError):
        guard.check("tank_level", 1.0)
    log = guard.audit_log()
    assert len(log) == 2
    assert log[0]["accepted"] is True
    assert log[0]["applied"] == 25.0
    assert log[1]["accepted"] is False
    assert "NotAllowListedError" in log[1]["detail"]


def test_clamped_writes_are_marked_in_the_audit(guard):
    """You need to be able to see that the value you asked for was changed."""
    guard.check("conveyor_speed_sp", 999.0)
    assert guard.audit_log()[0]["detail"] == "clamped"
    assert guard.audit_log()[0]["applied"] == 45.0


def test_apply_performs_the_write_through_the_driver(guard, driver, plc, db):
    """The guard is the only path to a driver write in the demo and CLI."""
    approved = guard.apply(driver, "conveyor_speed_sp", 22.0, operator="tester")
    assert approved.value == 22.0
    assert plc.speed_command == pytest.approx(22.0)


def test_status_reports_the_policy_and_the_notice(guard):
    """The dashboard footer shows this, so it has to be honest."""
    status = guard.status()
    assert status["read_only"] is False
    assert status["allow_list"] == ["conveyor_speed_sp"]
    assert status["notice"] == SAFETY_NOTICE
    assert "NOT a safety instrumented system" in status["notice"]


def test_writable_tags_lists_what_would_actually_pass(db, clock):
    """A tag on the allow-list but read-only in the map does not count."""
    policy = WritePolicy().allowing({"conveyor_speed_sp", "tank_level"})
    guard = WriteGuard(db, policy, clock)
    assert guard.writable_tags() == ["conveyor_speed_sp"]


def test_policy_can_be_built_from_config(db, clock):
    """config/devices.yaml carries the safety section."""
    policy = WritePolicy.from_mapping(
        {
            "read_only": False,
            "allow_list": ["conveyor_speed_sp"],
            "require_confirmation": ["conveyor_speed_sp"],
            "max_writes_per_window": 2,
        }
    )
    guard = WriteGuard(db, policy, clock)
    assert guard.policy.max_writes_per_window == 2
    token = guard.issue_token("conveyor_speed_sp", 30.0)
    assert guard.check("conveyor_speed_sp", 30.0, token=token).value == 30.0
