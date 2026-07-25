"""Payment execution adapters — downstream of an ALLOW gate decision."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class PaymentResult:
    payment_id: str
    status: str
    mode: str
    amount_value: str
    amount_currency: str
    vendor: str
    invoice_number: str


class PaymentProvider(Protocol):
    """Execute a payment after the gate returns ALLOW."""

    def execute(self, proposed_action: dict[str, Any]) -> PaymentResult: ...


class TestPaymentProvider:
    """Deterministic test rail — no external network, always succeeds."""

    mode = "test"

    def execute(self, proposed_action: dict[str, Any]) -> PaymentResult:
        amount = proposed_action["amount"]
        return PaymentResult(
            payment_id=f"pay_test_{uuid.uuid4().hex[:16]}",
            status="succeeded",
            mode=self.mode,
            amount_value=str(amount["value"]),
            amount_currency=str(amount["currency"]),
            vendor=str(proposed_action["vendor"]),
            invoice_number=str(proposed_action["invoice_number"]),
        )


def build_payment_provider() -> PaymentProvider:
    mode = os.environ.get("AGENTGATE_PAYMENT_MODE", "test").strip().lower()
    if mode == "test":
        return TestPaymentProvider()
    raise ValueError(
        f"Unsupported AGENTGATE_PAYMENT_MODE={mode!r}; only 'test' is wired in v1."
    )
