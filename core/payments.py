"""Payment Protocol seller side: build the RequestPayment, verify the CommitPayment.

Price and recipient are decided here, server-side; nothing the buyer sends is trusted. FET is
verified with a ledger query (cosmpy), Stripe with a Checkout Session lookup. Both functions
are blocking: callers run them with asyncio.to_thread. Replay protection lives in the
registry (payments.tx_id is unique), not here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from uagents_core.contrib.protocols.payment import CommitPayment, Funds, RequestPayment

from core import config
from core.models import AuditPlan

log = logging.getLogger("canary.payments")

LEDGER_TIMEOUT_S = 20


@dataclass
class Verification:
    ok: bool
    reason: str = ""
    method: str = ""
    tx_id: str = ""


def rail_label() -> str:
    if config.PAYMENT_RAIL == "stripe":
        return "Stripe test mode"
    return "FET stable-testnet" if config.FET_USE_TESTNET else "FET mainnet"


def enabled() -> bool:
    if config.PAYMENT_RAIL == "stripe":
        return bool(config.STRIPE_SECRET_KEY and config.STRIPE_PUBLISHABLE_KEY)
    return config.PAYMENT_RAIL == "fet"


# --- request -----------------------------------------------------------------------------


def _stripe_session(audit_id: str, requester: str, description: str) -> dict[str, Any] | None:
    import stripe

    stripe.api_key = config.STRIPE_SECRET_KEY
    cents = int(Decimal(config.AUDIT_PRICE_USD) * 100)
    base = {
        "redirect_on_completion": "if_required",
        "payment_method_types": ["card"],
        "mode": "payment",
        "line_items": [{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Canary trust audit", "description": description},
                "unit_amount": cents,
            },
            "quantity": 1,
        }],
        "return_url": f"https://agentverse.ai/payment-success?session_id={{CHECKOUT_SESSION_ID}}&audit_id={audit_id}",
        "expires_at": int(time.time()) + 1800,
        "metadata": {"user_address": requester, "session_id": audit_id},
    }
    # Stripe API 2026-03-25 renamed ui_mode "embedded" to "embedded_page"; try the new one first.
    for ui_mode in ("embedded_page", "embedded"):
        try:
            s = stripe.checkout.Session.create(ui_mode=ui_mode, idempotency_key=f"{audit_id}:{ui_mode}", **base)
            return {"client_secret": s.client_secret, "id": s.id, "amount_cents": cents, "ui_mode": ui_mode}
        except Exception as exc:
            log.warning("stripe session (%s) failed: %s", ui_mode, exc)
    return None


def build_request(plan: AuditPlan, requester: str, wallet_address: str) -> RequestPayment | None:
    """RequestPayment for an audit plan, or None if the rail is not usable (caller shows a notice).
    `reference` is the audit id: the commit is matched to pending server-side state through it."""
    if not enabled():
        return None
    desc = f"Canary audit of {plan.target_name}: {len(plan.cases)} live tests"
    metadata: dict[str, Any] = {"agent": "Canary-Orchestrator", "audit_id": plan.audit_id}
    if config.PAYMENT_RAIL == "stripe":
        sess = _stripe_session(plan.audit_id, requester, desc)
        if sess is None:
            return None
        funds = [Funds(currency="USD", amount=f"{Decimal(config.AUDIT_PRICE_USD):.2f}", payment_method="stripe")]
        metadata["stripe"] = {
            "ui_mode": "embedded",
            "publishable_key": config.STRIPE_PUBLISHABLE_KEY,
            "client_secret": sess["client_secret"],
            "checkout_session_id": sess["id"],
            "amount_cents": str(sess["amount_cents"]),
            "currency": "usd",
        }
    else:
        funds = [Funds(currency="FET", amount=config.AUDIT_PRICE_FET, payment_method="fet_direct")]
        metadata["fet_network"] = "stable-testnet" if config.FET_USE_TESTNET else "mainnet"
        metadata["mainnet"] = "false" if config.FET_USE_TESTNET else "true"
        metadata["provider_agent_wallet"] = wallet_address
    return RequestPayment(
        accepted_funds=funds,
        recipient=wallet_address,
        deadline_seconds=config.PAYMENT_DEADLINE_S,
        reference=plan.audit_id or uuid4().hex[:12],
        description=desc,
        metadata=metadata,
    )


# --- verification --------------------------------------------------------------------------


def _amount_ok(paid: str, expected: str) -> bool:
    try:
        return Decimal(paid) >= Decimal(expected)
    except (InvalidOperation, TypeError):
        return False


def _verify_fet(tx_id: str, buyer: str, expected_amount: str, recipient: str) -> bool:
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FuturesTimeout

    from cosmpy.aerial.client import LedgerClient, NetworkConfig

    denom = "atestfet" if config.FET_USE_TESTNET else "afet"
    net = NetworkConfig.fetchai_stable_testnet() if config.FET_USE_TESTNET else NetworkConfig.fetchai_mainnet()
    need = int(Decimal(expected_amount) * 10**18)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            tx = pool.submit(LedgerClient(net).query_tx, tx_id).result(timeout=LEDGER_TIMEOUT_S)
    except FuturesTimeout:
        log.error("ledger query timed out for %s", tx_id)
        return False
    except Exception as exc:  # unknown tx hash raises NotFoundError
        log.warning("ledger query failed for %s: %s", tx_id, type(exc).__name__)
        return False
    if tx is None or (hasattr(tx, "is_successful") and not tx.is_successful()):
        return False
    events = getattr(tx, "events", None)
    transfer = events.get("transfer") if isinstance(events, dict) else None
    if not isinstance(transfer, dict):
        return False
    amount = str(transfer.get("amount") or "")
    if not amount.endswith(denom):
        return False
    try:
        got = int(amount.removesuffix(denom))
    except ValueError:
        return False
    return (
        str(transfer.get("recipient") or "") == recipient
        and str(transfer.get("sender") or "") == buyer
        and got >= need
    )


def _verify_stripe(session_id: str, audit_id: str, requester: str) -> Verification:
    import stripe

    stripe.api_key = config.STRIPE_SECRET_KEY
    try:
        s = stripe.checkout.Session.retrieve(session_id)
    except Exception as exc:
        return Verification(False, f"Stripe lookup failed: {type(exc).__name__}")
    meta = getattr(s, "metadata", None) or {}
    meta = meta.to_dict_recursive() if hasattr(meta, "to_dict_recursive") else dict(meta)
    if getattr(s, "payment_status", None) != "paid":
        return Verification(False, "Stripe session is not paid")
    if meta.get("session_id") != audit_id or meta.get("user_address") != requester:
        return Verification(False, "Stripe session belongs to a different audit or user")
    if int(getattr(s, "amount_total", 0) or 0) < int(Decimal(config.AUDIT_PRICE_USD) * 100):
        return Verification(False, "Stripe amount is below the audit price")
    return Verification(True, method="stripe", tx_id=session_id)


def verify_commit(commit: CommitPayment, requester: str, audit_id: str, wallet_address: str) -> Verification:
    """Blocking. Checks method, currency, amount and the transaction itself. Never trusts the
    buyer's claimed amount: the price comes from config."""
    tx_id = (commit.transaction_id or "").strip()
    method = commit.funds.payment_method
    if not tx_id:
        return Verification(False, "missing transaction id")
    if config.PAYMENT_RAIL == "stripe":
        if method != "stripe":
            return Verification(False, f"unsupported payment method {method}")
        return _verify_stripe(tx_id, audit_id, requester)
    if config.PAYMENT_RAIL != "fet":
        return Verification(False, "payments are disabled")
    if method != "fet_direct" or commit.funds.currency != "FET":
        return Verification(False, f"unsupported payment method {method}/{commit.funds.currency}")
    if not _amount_ok(commit.funds.amount, config.AUDIT_PRICE_FET):
        return Verification(False, f"amount {commit.funds.amount} is below the price {config.AUDIT_PRICE_FET}")
    meta = commit.metadata if isinstance(commit.metadata, dict) else {}
    buyer = meta.get("buyer_fet_wallet") or meta.get("buyer_fet_address")
    if not isinstance(buyer, str) or not buyer:
        return Verification(False, "missing buyer_fet_wallet in metadata")
    if not _verify_fet(tx_id, buyer, config.AUDIT_PRICE_FET, wallet_address):
        return Verification(False, "the transaction could not be verified on the ledger")
    return Verification(True, method="fet_direct", tx_id=tx_id)
