"""A tiny stand-in for the Dodo Payments API, installed at `payments._dodo_http` so the real session
creation and verification code runs against it. Mirrors only the three calls Canary makes."""

from __future__ import annotations

from typing import Any
from uuid import uuid4


class FakeDodo:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.payments: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail = False  # simulate Dodo being down

    def __call__(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((method, path))
        if self.fail:
            raise ConnectionError("dodo is down")
        if method == "POST" and path == "/checkouts":
            assert body and body["product_cart"][0]["product_id"], "a product is required"
            sid = f"cks_{uuid4().hex[:12]}"  # real Dodo ids are globally unique
            self.sessions[sid] = {"id": sid, "payment_id": None, "payment_status": None, "metadata": body["metadata"]}
            return {"session_id": sid, "checkout_url": f"https://test.checkout.dodopayments.com/session/{sid}"}
        if method == "GET" and path.startswith("/checkouts/"):
            s = self.sessions[path.rsplit("/", 1)[1]]
            return {k: s[k] for k in ("id", "payment_id", "payment_status")}
        if method == "GET" and path.startswith("/payments/"):
            return self.payments[path.rsplit("/", 1)[1]]
        raise AssertionError(f"unexpected call {method} {path}")

    def pay(
        self, session_id: str, *, amount: int = 100, currency: str = "USD", status: str = "succeeded",
        bound_to: str | None = None, metadata: dict[str, str] | None = None,
    ) -> str:
        """The buyer completes checkout. Returns the payment id."""
        s = self.sessions[session_id]
        pid = f"pay_{uuid4().hex[:12]}"
        self.payments[pid] = {
            "payment_id": pid, "status": status, "total_amount": amount, "currency": currency,
            "checkout_session_id": bound_to or session_id,
            "metadata": s["metadata"] if metadata is None else metadata,
        }
        s["payment_id"], s["payment_status"] = pid, status
        return pid
