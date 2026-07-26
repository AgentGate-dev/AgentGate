"""Signed decision tokens + execution primitives (PRD Slice 10, D51–D53).

The token is the decide→execute bridge: HMAC-SHA256 over a canonical payload
binding the ENTIRE validated ProposedAction plus decision/trace_id/iat/exp.
Any parse, signature, alg, or expiry failure is a typed ``TokenError`` and a
refusal — never an execution, never a crash.
"""

from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agentgate.core.duplicate_store import DuplicateStore, TokenAlreadyConsumedError
from agentgate.core.execution import (
    ExecutionRequest,
    SandboxExecutor,
    SigningKeyError,
    TokenError,
    canonical_action_dump,
    default_executors,
    mint_token,
    signing_key_from_env,
    verify_token,
)
from agentgate.core.policy import PolicyError, load_policy
from agentgate.core.schemas import Money, ProposedAction

KEY = "k" * 32
OTHER_KEY = "x" * 32
NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


def clock_at(moment: datetime):
    return lambda: moment


def make_action(**overrides) -> ProposedAction:
    payload = {
        "action_type": "approve_payment",
        "invoice_number": "INV-001",
        "amount": {"value": "1240.00", "currency": "USD"},
        "vendor": "Acme Corp",
        "adjustments": [],
        "agent_rationale": "ok",
    }
    payload.update(overrides)
    return ProposedAction.model_validate(payload)


def mint(action=None, *, key=KEY, ttl=300, now=NOW, decision="allow", trace_id="trace-1") -> str:
    return mint_token(
        action if action is not None else make_action(),
        decision=decision,
        trace_id=trace_id,
        key=key,
        ttl_seconds=ttl,
        now=clock_at(now),
    )


# --- mint / verify -----------------------------------------------------------------


def test_token_roundtrip_binds_the_canonical_action():
    action = make_action()
    payload = verify_token(mint(action), key=KEY, now=clock_at(NOW))
    assert payload["alg"] == "HS256"
    assert payload["decision"] == "allow"
    assert payload["trace_id"] == "trace-1"
    # The FULL validated action rides inside the signed payload (D51) — the
    # executor re-checks that what it executes is exactly what was verified.
    assert payload["action"] == json.loads(canonical_action_dump(action))
    assert payload["action"]["amount"]["value"] == "1240.00"  # Decimal as string (D1)


def test_canonical_dump_is_stable_and_sorted():
    a = canonical_action_dump(make_action())
    b = canonical_action_dump(make_action())
    assert a == b
    assert a.index('"action_type"') < a.index('"amount"') < a.index('"vendor"')


def test_garbage_tokens_are_typed_refusals():
    for bad in ["", "not-a-token", "a.b.c", "onlyonepart", "!!.!!", "YQ==."]:
        with pytest.raises(TokenError):
            verify_token(bad, key=KEY, now=clock_at(NOW))


def test_wrong_key_signature_is_refused():
    token = mint(key=OTHER_KEY)
    with pytest.raises(TokenError, match="signature"):
        verify_token(token, key=KEY, now=clock_at(NOW))


def test_tampered_body_is_refused():
    token = mint()
    body_b64, sig_b64 = token.split(".")
    padded = body_b64 + "=" * (-len(body_b64) % 4)
    body = json.loads(base64.urlsafe_b64decode(padded))
    body["action"]["amount"]["value"] = "9999.00"
    forged_body = (
        base64.urlsafe_b64encode(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )
    with pytest.raises(TokenError, match="signature"):
        verify_token(f"{forged_body}.{sig_b64}", key=KEY, now=clock_at(NOW))


def test_alg_lives_inside_the_signed_payload_and_only_hs256_is_accepted():
    # Even a token correctly SIGNED with the right key is refused if its alg
    # field is not HS256 — the alg-confusion door stays closed (D52).
    action = make_action()
    payload = {
        "alg": "none",
        "trace_id": "trace-1",
        "decision": "allow",
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(seconds=300)).isoformat(),
        "action": json.loads(canonical_action_dump(action)),
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    sig = hmac_mod.new(KEY.encode(), body, hashlib.sha256).digest()
    forged = (
        base64.urlsafe_b64encode(body).decode().rstrip("=")
        + "."
        + base64.urlsafe_b64encode(sig).decode().rstrip("=")
    )
    with pytest.raises(TokenError, match="alg"):
        verify_token(forged, key=KEY, now=clock_at(NOW))


def test_expired_token_is_refused():
    token = mint(ttl=300)
    late = clock_at(NOW + timedelta(seconds=301))
    with pytest.raises(TokenError, match="expired"):
        verify_token(token, key=KEY, now=late)
    # Just inside the window still verifies.
    assert verify_token(token, key=KEY, now=clock_at(NOW + timedelta(seconds=299)))


# --- signing key rules -------------------------------------------------------------


def test_signing_key_requires_32_chars_and_has_no_default(monkeypatch):
    monkeypatch.delenv("AGENTGATE_SIGNING_KEY", raising=False)
    with pytest.raises(SigningKeyError, match="AGENTGATE_SIGNING_KEY"):
        signing_key_from_env()
    monkeypatch.setenv("AGENTGATE_SIGNING_KEY", "short")
    with pytest.raises(SigningKeyError, match="32"):
        signing_key_from_env()
    monkeypatch.setenv("AGENTGATE_SIGNING_KEY", KEY)
    assert signing_key_from_env() == KEY


# --- single-use consumption (same store object as duplicates, D52/D33) -------------


def test_consume_token_is_single_use():
    store = DuplicateStore()
    store.consume_token("trace-1", consumed_at="2026-07-26T12:00:00+00:00")
    with pytest.raises(TokenAlreadyConsumedError):
        store.consume_token("trace-1")


def test_store_knows_whether_it_is_file_backed(tmp_path):
    assert not DuplicateStore().is_file_backed
    assert DuplicateStore(str(tmp_path / "gate.db")).is_file_backed


def test_consumed_tokens_share_the_duplicate_store_connection(tmp_path):
    # One store object, one connection, both tables (D33 extended): a second
    # store on the same FILE sees both the reservation and the consumption.
    path = str(tmp_path / "gate.db")
    first = DuplicateStore(path)
    first.consume_token("trace-9")
    first.mark_approved("INV-9")
    second = DuplicateStore(path)
    assert second.is_approved("INV-9")
    with pytest.raises(TokenAlreadyConsumedError):
        second.consume_token("trace-9")


# --- executors ---------------------------------------------------------------------


def test_sandbox_executor_is_deterministic_and_referenced_by_trace():
    result = SandboxExecutor().execute(
        ExecutionRequest(action=make_action(), trace_id="trace-42")
    )
    assert result.executed is True
    assert result.executor == "sandbox"
    assert result.reference == "sandbox-trace-42"


def test_registry_covers_exactly_the_verifiable_action_type():
    registry = default_executors()
    assert set(registry) == {"approve_payment"}


# --- policy: execution section -----------------------------------------------------


def test_default_policy_carries_token_ttl():
    policy = load_policy()
    assert policy.execution.token_ttl_seconds == 300


def test_policy_rejects_unknown_execution_keys(tmp_path):
    bad = tmp_path / "p.yaml"
    bad.write_text(
        "escalate_if:\n  amount_greater_than: 10000\n  score_below: 0.80\n"
        "critical_checks: [action_type_supported, invoice_number_match, "
        "structural_arithmetic, currency_match, action_amount_matches_total]\n"
        "retry:\n  max_attempts: 2\n"
        "execution:\n  token_ttl_secondz: 60\n"
    )
    with pytest.raises(PolicyError, match="token_ttl_secondz"):
        load_policy(bad)


def test_policy_execution_ttl_is_configurable(tmp_path):
    good = tmp_path / "p.yaml"
    good.write_text(
        "escalate_if:\n  amount_greater_than: 10000\n  score_below: 0.80\n"
        "critical_checks: [action_type_supported, invoice_number_match, "
        "structural_arithmetic, currency_match, action_amount_matches_total]\n"
        "retry:\n  max_attempts: 2\n"
        "execution:\n  token_ttl_seconds: 60\n"
    )
    assert load_policy(good).execution.token_ttl_seconds == 60
