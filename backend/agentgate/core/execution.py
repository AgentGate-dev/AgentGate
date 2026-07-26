"""Signed decision tokens + payment executors (PRD Slice 10, D51–D53).

The token is the decide→execute bridge, not a bearer asset: HMAC-SHA256 over a
canonical JSON payload binding ``alg``, ``trace_id``, ``decision``,
``issued_at``/``expires_at``, and the **entire validated ProposedAction**
(sorted keys, Decimals as strings — D1). Deliberately not JWT: alg-confusion
history and a dependency buy nothing over this stdlib page; ``alg`` lives
*inside* the signed payload and only HS256 is accepted in v1 (Ed25519
externally-verifiable receipts are the named later milestone — the ``alg``
field is its slot). Any parse, signature, alg, or expiry failure is a typed
``TokenError`` → refusal; execution never proceeds on a bad token.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Protocol

from agentgate.core.schemas import ProposedAction

TOKEN_ALG = "HS256"
MIN_SIGNING_KEY_LENGTH = 32

Clock = Callable[[], datetime]


class TokenError(ValueError):
    """A decision token failed to parse, verify, or is expired. Always a
    refusal, never an execution (D52)."""


class SigningKeyError(ValueError):
    """No usable signing key. There is deliberately NO default key — a default
    would be unsigned execution in disguise (D52)."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def signing_key_from_env() -> str:
    """Read ``AGENTGATE_SIGNING_KEY``; require at least 32 chars, no default."""
    key = os.environ.get("AGENTGATE_SIGNING_KEY", "")
    if len(key) < MIN_SIGNING_KEY_LENGTH:
        raise SigningKeyError(
            "AGENTGATE_SIGNING_KEY is not configured (need at least 32 characters). "
            "pay_invoice refuses to execute without a signing key; the advisory "
            "verify_action tool is unaffected."
        )
    return key


def canonical_action_dump(action: ProposedAction) -> str:
    """The canonical JSON form of a validated action: sorted keys, compact
    separators, Decimals as strings (D1). This exact string is what the token
    signature covers — and what the executor re-checks before executing."""
    return json.dumps(action.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(part: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    except (binascii.Error, ValueError) as exc:
        raise TokenError(f"token part is not valid base64url: {exc}") from exc


def _sign(body: bytes, key: str) -> bytes:
    return hmac.new(key.encode("utf-8"), body, hashlib.sha256).digest()


def mint_token(
    action: ProposedAction,
    *,
    decision: str,
    trace_id: str,
    key: str,
    ttl_seconds: int,
    now: Clock = utcnow,
) -> str:
    """Mint a signed single-use decision token for ``action``."""
    issued = now()
    payload = {
        "alg": TOKEN_ALG,
        "trace_id": trace_id,
        "decision": decision,
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(seconds=ttl_seconds)).isoformat(),
        "action": json.loads(canonical_action_dump(action)),
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{_b64url(body)}.{_b64url(_sign(body, key))}"


def verify_token(token: str, *, key: str, now: Clock = utcnow) -> dict:
    """Verify signature (constant-time), alg, and expiry; return the payload.

    The signature is checked over the raw bytes BEFORE anything is parsed, so a
    forged body never reaches the JSON layer; the ``alg`` assertion afterwards
    guards the future multi-alg world (an attacker cannot change ``alg``
    without breaking the signature they are trying to downgrade)."""
    parts = token.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise TokenError("token must be base64url(payload).base64url(signature).")
    body = _b64url_decode(parts[0])
    signature = _b64url_decode(parts[1])
    if not hmac.compare_digest(signature, _sign(body, key)):
        raise TokenError("token signature does not verify.")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise TokenError(f"token payload is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TokenError("token payload must be a JSON object.")
    if payload.get("alg") != TOKEN_ALG:
        raise TokenError(f"unsupported token alg {payload.get('alg')!r}; only {TOKEN_ALG}.")
    try:
        expires_at = datetime.fromisoformat(str(payload["expires_at"]))
    except (KeyError, ValueError) as exc:
        raise TokenError(f"token expiry is missing or unreadable: {exc}") from exc
    if now() > expires_at:
        raise TokenError("token expired.")
    return payload


@dataclass(frozen=True)
class ExecutionRequest:
    """What an executor receives: the validated action plus the decision
    ``trace_id`` as the idempotency key for downstream rails (D53)."""

    action: ProposedAction
    trace_id: str


@dataclass(frozen=True)
class ExecutionResult:
    executed: bool
    executor: str
    reference: Optional[str] = None


class PaymentExecutor(Protocol):
    """Execute a verified action (the ``SourceOfRecord`` pattern, D53)."""

    def execute(self, request: ExecutionRequest) -> ExecutionResult: ...


class SandboxExecutor:
    """Reference executor: deterministic, no network, no new persistence
    surface — the durable record is the token store + duplicate store + trace
    (a parallel ledger file would be a second source of truth, D47)."""

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult(
            executed=True, executor="sandbox", reference=f"sandbox-{request.trace_id}"
        )


def default_executors() -> dict[str, PaymentExecutor]:
    """Registry keyed by action_type. v1 registers only ``approve_payment`` —
    the frame stage (F1) already escalates every other action_type before an
    ALLOW can exist, so a dispatch miss is defensive dead-man code: it refuses,
    never crashes (D53)."""
    return {"approve_payment": SandboxExecutor()}
