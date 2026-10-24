---
name: payment-protocol
description: Self-contained recipe for adding BOTH Stripe Checkout (card) and direct on-chain FET payments to a Fetch.ai uAgent alongside Chat Protocol, in the same agent, behind the same chat handler. Includes copy-paste-ready code for the Stripe helper, the FET ledger verifier, a single combined payment protocol that branches on `Funds.payment_method`, the chat protocol that triggers payment, the agent entrypoint, and `.env.example`. Covers `RequestPayment` with multi-method `accepted_funds`, `CommitPayment` dispatch, server-side verification (Stripe `Session.retrieve` + `cosmpy` ledger query), idempotency on `transaction_id`, and the right failure reply per method (`RejectPayment` for Stripe, `CancelPayment` for FET). Use whenever a uAgent should accept card AND crypto in one chat flow, or when wiring `payment_protocol_spec` next to `chat_protocol_spec` with multiple payment methods. Always use this skill when the user mentions "payment protocol", "accept Stripe and FET", "card or crypto", "multi-method payment", or any combination of `RequestPayment` / `CommitPayment` with both Stripe and FET; do not split the work across the single-method skills when both methods are wanted in the same agent.
---

# Payment Protocol (Stripe + FET, Fetch.ai uAgents)

## Purpose

Give the coding agent everything it needs — file layout, full code, env vars — to add a working **multi-method payment protocol** to a Fetch.ai uAgent that also speaks Chat Protocol. The agent advertises both Stripe Checkout (card) **and** direct on-chain FET (`fet_direct`) in a single `RequestPayment.accepted_funds`, then verifies whichever method the buyer chose before fulfilling.

This skill supersedes the two single-method skills (`stripe-payment-protocol`, `fet-payment-protocol`) when both methods are needed in the same agent. Do not "merge" the two single-method skills by hand — use this combined recipe instead, because the dispatch in `on_commit` and the dual `accepted_funds` shape need to match exactly.

## When to Use

- Building or extending a chat-capable uAgent that should accept BOTH Stripe card payments AND on-chain FET payments
- Implementing `uagents_core.contrib.protocols.payment` as a seller with multi-method `accepted_funds`
- Gating a chat action (image gen, video gen, paid API call, etc.) behind payment with the buyer choosing card or crypto
- Any task referencing `RequestPayment`, `CommitPayment`, `CompletePayment`, `RejectPayment`, `CancelPayment`, or `payment_protocol_spec` together with **both** Stripe and FET / `fet_direct`

## When NOT to Use

- Card-only flow with no crypto — use `stripe-payment-protocol`
- FET-only flow with no card — use `fet-payment-protocol`
- Pure web-app Stripe integration with no uAgents
- Mock / pseudo payment flows
- Stripe subscriptions (`mode="subscription"`) — this skill targets one-time Checkout Sessions
- Smart-contract escrow, staking, or non-FET denoms on Fetch.ai (USDC, etc.)

---

## Package Setup

Before running install commands, creating `pyproject.toml`, or generating `.env.example` / `.gitignore`, check which **package setup skill** is installed and defer to it:

- `uv-package` for `uv` projects
- `poetry-package` for Poetry projects
- `python-venv-package` for `venv` + `pip` projects
- `no-package` for code-only mode (no install commands)

This protocol skill only defines payment protocol, Stripe integration, and FET ledger verification code. It does not own dependency installation, lockfile management, or virtualenv creation.

### Package Skill Precedence (mandatory)

Use this decision order before emitting any setup/install/run command:

1. Detect which package skills are actually available in the current session from this set only: `uv-package`, `no-package`, `poetry-package`, `python-venv-package`.
2. If the user explicitly asks for one of those managers and the matching skill is available, use it.
3. Otherwise, if repository signals are present, select the matching available skill:
   - `uv.lock` or `[tool.uv]` -> `uv-package`
   - `poetry.lock` or `[tool.poetry]` -> `poetry-package`
   - `requirements.txt` / existing `.venv` conventions -> `python-venv-package`
4. If no manager is detectable, choose one available skill and stay consistent for the full response. Prefer this fallback order: `uv-package` -> `poetry-package` -> `python-venv-package` -> `no-package`.
5. Never emit commands for a package skill that is not available.

When a package skill is active, all dependency/setup/run instructions must come from that skill only:

- `uv-package` -> `uv add ...`, `uv run python agent.py`
- `poetry-package` -> `poetry add ...`, `poetry run python agent.py`
- `python-venv-package` -> `pip install ...` (and project's venv flow)
- `no-package` -> no install commands; only document dependencies

Extra runtime dependencies required by this skill on top of the base set (`uagents`, `uagents-core`, `python-dotenv`): `stripe`, `cosmpy`, and `openai` (ASI:One default LLM client). Add them through whichever package skill is active:

| Active package skill | Command |
| --- | --- |
| `uv-package` | `uv add stripe cosmpy openai` |
| `poetry-package` | `poetry add stripe cosmpy openai` |
| `python-venv-package` | `pip install stripe cosmpy openai && pip freeze > requirements.txt` |
| `no-package` | document `stripe`, `cosmpy`, and `openai` in the project's `## Dependencies` block |

`stripe` and `cosmpy` are required regardless of which method any individual buyer chooses, because the agent advertises both upfront and must be able to verify whichever one is committed. `openai` is required because `run_paid_action` ships ASI:One-backed by default.

---

## File Layout

Build this structure exactly. Every file has a single concern:

```
agent.py                       # entrypoint: load env, create Agent, set wallet, include both protocols
protocols/
  __init__.py                  # empty
  chat_proto.py                # chat protocol + handler that triggers payment when needed
  payment_proto.py             # combined payment protocol + handlers + request_payment_from_user helper
stripe_payments/
  __init__.py                  # empty
  checkout.py                  # ONLY place that touches the Stripe SDK
fet_payments/
  __init__.py                  # empty
  ledger.py                    # ONLY place that touches cosmpy / the Fetch.ai ledger
.env.example
```

Hard separation — never mix:

- Stripe SDK calls live **only** in `stripe_payments/checkout.py`.
- `cosmpy` calls and ledger queries live **only** in `fet_payments/ledger.py`.
- `RequestPayment` / `CommitPayment` / `RejectPayment` / `CancelPayment` / `CompletePayment` handling lives **only** in `protocols/payment_proto.py` (one file, branches by `payment_method`).
- `ChatMessage` handling lives **only** in `protocols/chat_proto.py`.
- `agent.py` only loads env, creates the Agent, calls `set_agent_wallet(agent.wallet)`, and `include`s both protocols.

> Naming notes: do **not** name the local Stripe folder `stripe/` — it shadows the official `stripe` SDK on `import stripe` and breaks every Stripe API call. Use `stripe_payments/`. Likewise do not create a top-level `cosmpy/` or `fetchai/` package; those shadow their SDKs the same way.

---

## The Combined Payment Flow

```
ChatMessage arrives
  protocols/chat_proto.py: handle_chat
    └─ if user already has verified_payment for this session -> fulfill paid action
    └─ if request needs payment -> call request_payment_from_user(...)

request_payment_from_user (in protocols/payment_proto.py)
  └─ persist user's prompt under "chat_prompt:<user>:<chat_session_id>"
  └─ stripe_payments.checkout.create_embedded_checkout_session(...)
  └─ build accepted_funds = [
       Funds(currency="USD", amount=..., payment_method="stripe"),
       Funds(currency="FET", amount=..., payment_method="fet_direct"),
     ]
  └─ build metadata that contains BOTH:
       {
         "stripe":   { client_secret, checkout_session_id, publishable_key, ... },
         "fet_network": "stable-testnet" | "mainnet",
         "mainnet":     "false" | "true",
         "provider_agent_wallet": str(agent.wallet.address()),
       }
  └─ ctx.send(user_address, RequestPayment(...))
  └─ persist pending state under (user, session)

User picks card OR FET in their wallet/UI and submits CommitPayment.

CommitPayment arrives -> on_commit dispatches by msg.funds.payment_method:
  "stripe"      -> verify_checkout_session_paid(transaction_id)
                   -> resolve session_id (Stripe metadata -> by_checkout map -> latest pending paid session -> ctx.session)
                   -> CompletePayment + run_paid_action(prompt) | RejectPayment
  "fet_direct"  -> verify_fet_payment_to_agent(...)
                   -> CompletePayment + run_paid_action(prompt) | CancelPayment
  anything else -> RejectPayment("unsupported payment method")

If buyer/UI sends RejectPayment -> on_reject clears pending state and notifies via chat.
```

Why two different failure reply types? The single-method skills already established conventions the Agentverse UI understands: Stripe failures are reported via `RejectPayment(reason=...)`; FET failures are reported via `CancelPayment(transaction_id=..., reason=...)`. Keep them split — do **not** "unify" by replying with the same type for both methods.

The seller agent never holds buyer keys and never collects card data itself. The Agentverse / wallet UI uses `metadata["stripe"]["client_secret"]` to mount Stripe's embedded Checkout when the buyer picks card; the same UI uses `metadata["provider_agent_wallet"]` and `fet_network` to broadcast the FET transfer when the buyer picks crypto.

---

## Directional Logging Contract

Every agent generated from this skill must emit structured logs so the user can trace the full chat + multi-method payment lifecycle directly from CLI output. This makes production debugging tractable and keeps the dispatch in `on_commit` auditable end-to-end.

### Required tags

- `[inbound]` — log on entry to every protocol handler (`ChatMessage`, `ChatAcknowledgement`, `CommitPayment`, `RejectPayment`), **before** any processing. Always include message type + sender.
- `[outbound]` — log immediately **before** every `ctx.send(...)`. Always include message type + recipient.
- `[state]` — log significant storage transitions (pending request set/cleared, verified-payment set/cleared, duplicate short-circuits).
- `[payment]` — domain-specific events. Always prefix with the method so dispatch is visible: `[payment] stripe ...` or `[payment] fet ...`.
- `[error]` — `ctx.logger.error(...)` with context; never swallow failures silently.

### Redaction rules

- Never log `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, the Stripe `client_secret`, `AGENT_SEED`, raw private keys, full signed transactions, or full `msg.metadata` dumps.
- Stripe: log only Checkout Session ID (`cs_...`), `payment_method`, amount + currency, and `payment_status`.
- FET: log only `tx_id`, `buyer_fet_wallet`, `recipient_agent_wallet`, amount in FET + denom, and network (`mainnet` / `stable-testnet`).
- Truncate user-supplied description / chat text at ~120 chars.

### Helpers (put in `protocols/payment_proto.py`; `protocols/chat_proto.py` imports them)

```python
def log_inbound(ctx: Context, msg_type: str, sender: str, details: str | None = None) -> None:
    suffix = f" {details}" if details else ""
    ctx.logger.info(f"[inbound] {msg_type} from {sender}{suffix}")


def log_outbound(ctx: Context, msg_type: str, recipient: str, details: str | None = None) -> None:
    suffix = f" {details}" if details else ""
    ctx.logger.info(f"[outbound] {msg_type} -> {recipient}{suffix}")


def log_state(ctx: Context, event: str, details: str | None = None) -> None:
    suffix = f" {details}" if details else ""
    ctx.logger.info(f"[state] {event}{suffix}")
```

Every `ctx.send(...)` in payment and chat code must be preceded by `log_outbound(...)`. Every `@payment_proto.on_message(...)` / `@chat_proto.on_message(...)` handler must start with `log_inbound(...)`.

---

## File 1 — `stripe_payments/checkout.py`

The only file that touches the Stripe SDK. Pure functions, no `ctx`, no protocol types.

```python
import os
import time
from typing import Any
from uuid import uuid4


def _stripe_sdk():
    try:
        import stripe
        return stripe
    except Exception:
        return None


def is_stripe_configured() -> bool:
    sdk = _stripe_sdk()
    return bool(sdk and os.getenv("STRIPE_SECRET_KEY") and os.getenv("STRIPE_PUBLISHABLE_KEY"))


def stripe_metadata_to_dict(metadata: Any) -> dict[str, Any]:
    """Stripe returns metadata as a StripeObject in some SDK versions, not a dict."""
    if not metadata:
        return {}
    if isinstance(metadata, dict):
        return metadata
    if hasattr(metadata, "to_dict_recursive"):
        return metadata.to_dict_recursive()
    try:
        return dict(metadata)
    except Exception:
        return {}


def create_embedded_checkout_session(
    *,
    user_address: str,
    chat_session_id: str,
    description: str,
    payment_request_id: str | None = None,
    amount_cents_override: int | None = None,
) -> dict[str, Any] | None:
    sdk = _stripe_sdk()
    if not sdk:
        return None
    secret = os.getenv("STRIPE_SECRET_KEY")
    publishable = os.getenv("STRIPE_PUBLISHABLE_KEY")
    if not (secret and publishable):
        return None
    sdk.api_key = secret

    price_id = (os.getenv("STRIPE_PRICE_ID") or "").strip()
    amount_cents = amount_cents_override if amount_cents_override is not None else int(
        os.getenv("STRIPE_AMOUNT_CENTS", "100")
    )
    currency = (os.getenv("STRIPE_CURRENCY") or "usd").strip().lower()
    product_name = os.getenv("STRIPE_PRODUCT_NAME", "Agent service")
    success_url = os.getenv("STRIPE_SUCCESS_URL", "https://agentverse.ai/payment-success")

    expires_in = max(1800, min(24 * 60 * 60, int(os.getenv("STRIPE_CHECKOUT_EXPIRES_SECONDS", "1800"))))
    expires_at = int(time.time()) + expires_in

    payment_reference = f"{user_address}:{chat_session_id}:{uuid4().hex[:8]}"
    return_url = (
        f"{success_url}?session_id={{CHECKOUT_SESSION_ID}}"
        f"&chat_session_id={chat_session_id}&user={user_address}"
    )
    if payment_request_id:
        return_url += f"&payment_request_id={payment_request_id}"

    line_items = (
        [{"price": price_id, "quantity": 1}]
        if price_id
        else [{
            "price_data": {
                "currency": currency,
                "product_data": {"name": product_name, "description": description},
                "unit_amount": amount_cents,
            },
            "quantity": 1,
        }]
    )

    idem_parts = [
        user_address,
        chat_session_id,
        payment_request_id or "",
        "price" if price_id else "inline",
        str(amount_cents),
    ]
    base_idempotency_key = ":".join(idem_parts)

    base_kwargs: dict[str, Any] = {
        "redirect_on_completion": "if_required",
        "payment_method_types": ["card"],
        "mode": "payment",
        "line_items": line_items,
        "return_url": return_url,
        "expires_at": expires_at,
        "metadata": {
            "user_address": user_address,
            "session_id": chat_session_id,
            "payment_reference": payment_reference,
        },
    }

    # Stripe API 2026-03-25 ("Dahlia") renamed ui_mode "embedded" -> "embedded_page".
    # Try the new value first; fall back to the legacy value if the account is pinned
    # to an older API version. Each attempt uses its own idempotency key so Stripe
    # doesn't replay a cached 400 from the first try.
    ui_mode_attempts = [
        ("embedded_page", base_idempotency_key + ":embedded_page"),
        ("embedded", base_idempotency_key + ":embedded"),
    ]
    last_error: str | None = None

    for ui_mode, idem_key in ui_mode_attempts:
        try:
            session = sdk.checkout.Session.create(
                ui_mode=ui_mode,
                idempotency_key=idem_key[:255],
                **base_kwargs,
            )
            return {
                "client_secret": session.client_secret,
                "id": session.id,
                "publishable_key": publishable,
                "amount_cents": amount_cents,
                "currency": currency,
                "ui_mode": ui_mode,
            }
        except sdk.error.InvalidRequestError as exc:
            err = str(exc).lower()
            last_error = str(exc)
            if "ui_mode" in err and (
                "no longer supported" in err
                or "invalid value" in err
                or "is not a valid" in err
            ):
                continue
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": str(exc)}

    return {"error": last_error or "Could not create Checkout Session"}


def verify_checkout_session_paid(checkout_session_id: str) -> dict[str, Any]:
    sdk = _stripe_sdk()
    if not sdk:
        return {"verified": False, "error": "stripe SDK not installed"}
    secret = os.getenv("STRIPE_SECRET_KEY")
    if not secret:
        return {"verified": False, "error": "Stripe not configured"}
    sdk.api_key = secret
    try:
        s = sdk.checkout.Session.retrieve(checkout_session_id)
        metadata = stripe_metadata_to_dict(getattr(s, "metadata", {}) or {})
        return {
            "verified": getattr(s, "payment_status", None) == "paid",
            "checkout_session_id": checkout_session_id,
            "status": getattr(s, "payment_status", None),
            "amount_total": getattr(s, "amount_total", None),
            "currency": getattr(s, "currency", None),
            "metadata": metadata,
        }
    except Exception as exc:
        return {"verified": False, "error": str(exc)}
```

---

## File 2 — `fet_payments/ledger.py`

The only file that touches `cosmpy`. Pure functions, no `ctx`, no protocol types.

```python
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any


def extract_buyer_fet_wallet(metadata: Any) -> str | None:
    """Read the buyer's FET address from CommitPayment.metadata.

    Accepts either `buyer_fet_wallet` or `buyer_fet_address`.
    """
    if not isinstance(metadata, dict):
        return None
    v = metadata.get("buyer_fet_wallet") or metadata.get("buyer_fet_address")
    return v if isinstance(v, str) and v else None


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def verify_fet_payment_to_agent(
    *,
    transaction_id: str,
    expected_amount_fet: str,
    sender_fet_address: str,
    recipient_agent_wallet,        # any object with .address()
    logger,
    use_mainnet: bool | None = None,
) -> bool:
    """Verify a direct FET transfer by inspecting the chain transaction.

    `expected_amount_fet` is in FET units (string, e.g. "0.001").
    `recipient_agent_wallet` must expose `.address()` (e.g. `agent.wallet`).
    """
    try:
        prefer_mainnet = (os.getenv("FET_USE_TESTNET", "true").strip().lower() != "true")
        networks: list[bool] = (
            [use_mainnet] if use_mainnet is not None else [prefer_mainnet, not prefer_mainnet]
        )
        for net_is_mainnet in networks:
            if _verify_fet_tx(
                transaction_id=transaction_id,
                expected_amount_fet=expected_amount_fet,
                sender_fet_address=sender_fet_address,
                recipient_agent_wallet=recipient_agent_wallet,
                logger=logger,
                use_mainnet=net_is_mainnet,
            ):
                return True
        return False
    except Exception as e:
        logger.error(f"FET payment verification failed: {e}")
        return False


def _verify_fet_tx(
    *,
    transaction_id: str,
    expected_amount_fet: str,
    sender_fet_address: str,
    recipient_agent_wallet,
    logger,
    use_mainnet: bool,
) -> bool:
    expected_amount_micro = int(float(expected_amount_fet) * 10**18)
    denom = "afet" if use_mainnet else "atestfet"
    expected_recipient = str(recipient_agent_wallet.address())

    logger.info(
        f"Verifying FET payment of {expected_amount_fet} FET ({expected_amount_micro} {denom}) "
        f"from {sender_fet_address} to {expected_recipient} "
        f"on {'mainnet' if use_mainnet else 'testnet'}"
    )

    from cosmpy.aerial.client import LedgerClient, NetworkConfig

    network_config = (
        NetworkConfig.fetchai_mainnet() if use_mainnet else NetworkConfig.fetchai_stable_testnet()
    )
    ledger = LedgerClient(network_config)

    query_timeout_s = float(_env_int("FET_LEDGER_QUERY_TIMEOUT_SECONDS", 20))
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(ledger.query_tx, transaction_id)
            tx_resp = fut.result(timeout=query_timeout_s)
    except FuturesTimeoutError:
        logger.error(
            f"FET tx query timed out after {query_timeout_s:.0f}s on "
            f"{'mainnet' if use_mainnet else 'testnet'} (tx={transaction_id})"
        )
        return False

    if tx_resp is None:
        logger.error(f"Transaction {transaction_id} not found on {'mainnet' if use_mainnet else 'testnet'}")
        return False
    if hasattr(tx_resp, "is_successful") and not tx_resp.is_successful():
        logger.error(f"Transaction {transaction_id} was not successful")
        return False
    events = getattr(tx_resp, "events", None)
    if not isinstance(events, dict):
        logger.error(f"Transaction {transaction_id} has no usable events")
        return False

    valid_recipient = False
    valid_sender = False
    valid_amount = False

    transfer = events.get("transfer")
    if isinstance(transfer, dict):
        recipient = str(transfer.get("recipient") or "")
        sender = str(transfer.get("sender") or "")
        amount_str = str(transfer.get("amount") or "")
        if recipient == expected_recipient:
            valid_recipient = True
        if sender == sender_fet_address:
            valid_sender = True
        if amount_str.endswith(denom):
            try:
                if int(amount_str.replace(denom, "")) >= expected_amount_micro:
                    valid_amount = True
            except ValueError:
                pass

    if not (valid_recipient and valid_amount):
        coin_received = events.get("coin_received")
        if isinstance(coin_received, dict):
            if str(coin_received.get("receiver") or "") == expected_recipient:
                valid_recipient = True
            amount_str = str(coin_received.get("amount") or "")
            if amount_str.endswith(denom):
                try:
                    if int(amount_str.replace(denom, "")) >= expected_amount_micro:
                        valid_amount = True
                except ValueError:
                    pass

    if not valid_sender:
        coin_spent = events.get("coin_spent")
        if isinstance(coin_spent, dict) and str(coin_spent.get("spender") or "") == sender_fet_address:
            valid_sender = True

    if valid_recipient and valid_amount and valid_sender:
        logger.info(f"FET tx verified: {transaction_id}")
        return True

    logger.warning(
        "FET verification incomplete - "
        f"recipient={valid_recipient}, amount={valid_amount}, sender={valid_sender}"
    )
    return False
```

---

## File 3 — `protocols/payment_proto.py` (combined dispatcher)

Owns the single seller-side payment protocol, the multi-method `RequestPayment` builder, and one `on_commit` that branches on `msg.funds.payment_method`. Imports from BOTH `stripe_payments.checkout` and `fet_payments.ledger`. Never touches either SDK directly.

```python
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from uuid import uuid4

from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import ChatMessage, TextContent
from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    Funds,
    RejectPayment,
    RequestPayment,
    payment_protocol_spec,
)

from stripe_payments.checkout import (
    create_embedded_checkout_session,
    is_stripe_configured,
    verify_checkout_session_paid,
)
from fet_payments.ledger import extract_buyer_fet_wallet, verify_fet_payment_to_agent

payment_proto = Protocol(spec=payment_protocol_spec, role="seller")


def _env_int(name: str, default: int) -> int:
    """Safe int parsing for env vars; falls back to default on missing/garbage values
    instead of raising at module import time."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


# --- pricing knobs (computed server-side, never trusted from the buyer) ---
STRIPE_AMOUNT_CENTS = _env_int("STRIPE_AMOUNT_CENTS", 100)
STRIPE_CURRENCY = (os.getenv("STRIPE_CURRENCY") or "USD").strip().upper()
STRIPE_FUNDS = Funds(
    currency=STRIPE_CURRENCY,
    amount=f"{STRIPE_AMOUNT_CENTS / 100:.2f}",
    payment_method="stripe",
)

FET_AMOUNT_FET = (os.getenv("FET_AMOUNT_FET") or "0.001").strip() or "0.001"
FET_FUNDS = Funds(currency="FET", amount=FET_AMOUNT_FET, payment_method="fet_direct")

_agent_wallet = None  # set from agent.py via set_agent_wallet(agent.wallet)


def set_agent_wallet(wallet) -> None:
    global _agent_wallet
    _agent_wallet = wallet


def _env_true(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _use_testnet() -> bool:
    return os.getenv("FET_USE_TESTNET", "true").strip().lower() == "true"


def log_inbound(ctx: Context, msg_type: str, sender: str, details: str | None = None) -> None:
    suffix = f" {details}" if details else ""
    ctx.logger.info(f"[inbound] {msg_type} from {sender}{suffix}")


def log_outbound(ctx: Context, msg_type: str, recipient: str, details: str | None = None) -> None:
    suffix = f" {details}" if details else ""
    ctx.logger.info(f"[outbound] {msg_type} -> {recipient}{suffix}")


def log_state(ctx: Context, event: str, details: str | None = None) -> None:
    suffix = f" {details}" if details else ""
    ctx.logger.info(f"[state] {event}{suffix}")


def metadata_value(metadata, key: str, default: str = "") -> str:
    """Stripe metadata may be a StripeObject; normalize reads to avoid AttributeError."""
    if isinstance(metadata, dict):
        return str(metadata.get(key) or default)
    if hasattr(metadata, "to_dict_recursive"):
        return str(metadata.to_dict_recursive().get(key) or default)
    return str(getattr(metadata, key, default) or default)


def _lookup_session_id_by_checkout(ctx: Context, tx_id: str) -> str:
    """Recover the chat session id for a Stripe Checkout Session id.

    Stripe metadata occasionally arrives empty (legacy sessions, custom UIs
    that strip metadata). The pending request mapping persisted in
    `request_payment_from_user` is the canonical fallback.
    """
    try:
        rec = ctx.storage.get(f"payment_request:by_checkout:{tx_id}") or {}
    except Exception:
        return ""
    if isinstance(rec, dict):
        return str(rec.get("chat_session_id") or "")
    return ""


def _latest_session_key(user_address: str) -> str:
    return f"payment_request:latest_session:{user_address}"


def _resolve_latest_session_id(ctx: Context, user_address: str) -> str:
    """Best-effort session recovery for replayed commits / session drift."""
    try:
        rec = ctx.storage.get(_latest_session_key(user_address)) or {}
        if isinstance(rec, dict):
            sid = str(rec.get("session_id") or "").strip()
            if sid:
                return sid
    except Exception:
        pass
    return str(ctx.session)


def _fulfilled_key(user_address: str, session_id: str, tx_id: str) -> str:
    return f"payment:fulfilled:{user_address}:{session_id}:{tx_id}"


def _chat(text: str) -> ChatMessage:
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=[TextContent(type="text", text=text)],
    )


def _accepted_methods() -> list[Funds]:
    """Build the multi-method accepted_funds list, skipping anything disabled."""
    funds: list[Funds] = []
    if _env_true("ENABLE_STRIPE_PAYMENTS", True) and is_stripe_configured():
        funds.append(STRIPE_FUNDS)
    if _env_true("ENABLE_FET_PAYMENTS", True) and _agent_wallet is not None:
        funds.append(FET_FUNDS)
    return funds


async def request_payment_from_user(
    ctx: Context,
    user_address: str,
    description: str | None = None,
    text: str | None = None,
) -> None:
    """Build a single RequestPayment that advertises every enabled method.

    `text` is the user's original prompt. We persist it so the per-method
    `_on_commit_*` handler can fulfill the request after verification.
    """
    if _agent_wallet is None:
        ctx.logger.error("[payment] agent wallet not set; call set_agent_wallet(agent.wallet)")
        return

    chat_session_id = str(ctx.session)
    pending_key = f"payment_request:pending:{user_address}:{chat_session_id}"
    prompt_key = f"chat_prompt:{user_address}:{chat_session_id}"

    # Always refresh the persisted prompt so the latest paid request wins.
    try:
        ctx.storage.set(prompt_key, text or "")
        ctx.storage.set(_latest_session_key(user_address), {"session_id": chat_session_id, "ts": time.time()})
    except Exception as e:
        ctx.logger.warning(f"[payment] failed to persist chat prompt: {e}")

    pending_ttl_s = max(60, min(24 * 60 * 60, int(os.getenv("PAYMENT_REQUEST_PENDING_TTL_SECONDS", "1800"))))
    resend_min_s = max(10, min(3600, int(os.getenv("PAYMENT_REQUEST_RESEND_MIN_INTERVAL_SECONDS", "60"))))
    now = time.time()

    try:
        already_verified = bool(ctx.storage.get(f"{user_address}:{chat_session_id}:verified_payment"))
    except Exception:
        already_verified = False
    if already_verified:
        log_state(ctx, "already_verified", f"user={user_address} session={chat_session_id}")
        return

    try:
        pending = ctx.storage.get(pending_key) if ctx.storage.has(pending_key) else None
    except Exception:
        pending = None
    if isinstance(pending, dict):
        try:
            ts = float(pending.get("ts") or 0)
            last_sent = float(pending.get("last_sent") or 0)
        except Exception:
            ts, last_sent = 0.0, 0.0
        if ts and (now - ts) < pending_ttl_s and (now - last_sent) < resend_min_s:
            log_state(ctx, "pending_request_suppressed", f"user={user_address} session={chat_session_id}")
            return

    accepted = _accepted_methods()
    if not accepted:
        ctx.logger.warning("[payment] no payment methods enabled; cannot request payment")
        reply = _chat("Payments are not available right now.")
        log_outbound(ctx, "ChatMessage", user_address, "no_methods_enabled")
        await ctx.send(user_address, reply)
        return

    metadata: dict = {"agent": ctx.agent.name}

    # Stripe metadata + Checkout Session creation only when Stripe is in the offer.
    if any(f.payment_method == "stripe" for f in accepted):
        checkout = create_embedded_checkout_session(
            user_address=user_address,
            chat_session_id=chat_session_id,
            description=description or "Payment required to continue.",
            payment_request_id=uuid4().hex,
        )
        if not checkout or checkout.get("error") or not checkout.get("client_secret"):
            err = (checkout or {}).get("error") or "unknown error"
            ctx.logger.error(f"[payment] stripe checkout create failed: {err}")
            # Drop Stripe from the offer; FET (if enabled) can still proceed.
            accepted = [f for f in accepted if f.payment_method != "stripe"]
        else:
            ctx.logger.info(
                f"[payment] stripe checkout created cs_id={checkout['id']} "
                f"amount_cents={checkout['amount_cents']} currency={checkout['currency']} "
                f"ui_mode={checkout.get('ui_mode')}"
            )
            metadata["stripe"] = {
                # Literal "embedded" here is the Agentverse UI contract — NOT the
                # Stripe API enum (which moved to "embedded_page" in Dahlia).
                "ui_mode": "embedded",
                "publishable_key": checkout["publishable_key"],
                "client_secret": checkout["client_secret"],
                "checkout_session_id": checkout["id"],
                "amount_cents": checkout["amount_cents"],
                "currency": checkout["currency"],
            }

    # FET metadata only when FET is in the offer.
    if any(f.payment_method == "fet_direct" for f in accepted):
        use_testnet = _use_testnet()
        metadata["fet_network"] = "stable-testnet" if use_testnet else "mainnet"
        metadata["mainnet"] = "false" if use_testnet else "true"
        metadata["provider_agent_wallet"] = str(_agent_wallet.address())

    if not accepted:
        reply = _chat("No payment methods are available right now. Please try again later.")
        log_outbound(ctx, "ChatMessage", user_address, "no_methods_enabled")
        await ctx.send(user_address, reply)
        return

    deadline_seconds = max(60, min(24 * 60 * 60, int(os.getenv("CHECKOUT_DEADLINE_SECONDS", "300"))))
    msg = RequestPayment(
        accepted_funds=accepted,
        recipient=str(_agent_wallet.address()),
        deadline_seconds=deadline_seconds,
        reference=str(uuid4()),
        description=description or "Payment required to continue.",
        metadata=metadata,
    )

    method_summary = ",".join(f.payment_method for f in accepted)
    log_outbound(
        ctx, "RequestPayment", user_address,
        f"methods={method_summary} ref={msg.reference} recipient={msg.recipient}",
    )
    await ctx.send(user_address, msg)

    pending_payload = {
        "ts": now,
        "last_sent": now,
        "reference": msg.reference,
        "deadline_seconds": deadline_seconds,
        "description": msg.description,
        "recipient": msg.recipient,
        "metadata": {k: ("<redacted>" if k == "stripe" else v) for k, v in metadata.items()},
        "accepted_funds": [
            {"currency": f.currency, "amount": f.amount, "payment_method": f.payment_method}
            for f in accepted
        ],
    }
    try:
        ctx.storage.set(pending_key, pending_payload)
        if "stripe" in metadata:
            ctx.storage.set(
                f"payment_request:by_checkout:{metadata['stripe']['checkout_session_id']}",
                {
                    "user_address": user_address,
                    "chat_session_id": chat_session_id,
                    "amount_cents": int(metadata["stripe"]["amount_cents"]),
                    "currency": str(metadata["stripe"]["currency"]).lower(),
                },
            )
        log_state(
            ctx, "pending_request_saved",
            f"user={user_address} session={chat_session_id} ref={msg.reference} methods={method_summary}",
        )
    except Exception as e:
        ctx.logger.warning(f"[payment] failed to persist pending request: {e}")


# ---- Commit dispatch ----------------------------------------------------------

async def _resume_paid_fulfillment(
    ctx: Context,
    *,
    sender: str,
    session_id: str,
    tx_id: str,
    replay: bool,
) -> None:
    try:
        prompt = str(ctx.storage.get(f"chat_prompt:{sender}:{session_id}") or "")
    except Exception:
        prompt = ""
    try:
        from protocols.chat_proto import run_paid_action
        await run_paid_action(ctx, sender, session_id, text=prompt)
        ctx.storage.set(_fulfilled_key(sender, session_id, tx_id), True)
        replay_suffix = " replay=true" if replay else ""
        log_state(
            ctx,
            "post_payment_fulfilled",
            f"user={sender} session={session_id} tx_id={tx_id}{replay_suffix}",
        )
    except Exception as e:
        ctx.logger.exception(f"[payment] post-payment fulfillment failed: {e}")
        fail = _chat("Payment received, but generating the result failed. Please retry.")
        log_outbound(
            ctx,
            "ChatMessage",
            sender,
            "post_payment_fulfillment_failed_replay" if replay else "post_payment_fulfillment_failed",
        )
        await ctx.send(sender, fail)


async def _on_commit_stripe(ctx: Context, sender: str, msg: CommitPayment) -> None:
    tx_id = str(getattr(msg, "transaction_id", "") or "")
    if not tx_id:
        log_outbound(ctx, "RejectPayment", sender, "reason=missing_tx_id method=stripe")
        await ctx.send(sender, RejectPayment(reason="Missing Stripe checkout session id"))
        return

    result = verify_checkout_session_paid(tx_id)
    ctx.logger.info(
        f"[payment] stripe verify tx_id={tx_id} verified={result.get('verified')} "
        f"status={result.get('status')} amount_total={result.get('amount_total')} "
        f"currency={result.get('currency')}"
    )
    if not result.get("verified"):
        reason = result.get("error") or "Stripe payment not completed yet."
        log_outbound(ctx, "RejectPayment", sender, f"reason=not_verified tx_id={tx_id}")
        await ctx.send(sender, RejectPayment(reason=reason))
        return

    # Resolve the chat session id with a robust fallback chain so a verified
    # payment never gets rejected just because Stripe's metadata round-trip
    # came back empty.
    metadata = result.get("metadata") or {}
    session_id = (
        metadata_value(metadata, "session_id")
        or _lookup_session_id_by_checkout(ctx, tx_id)
        or _resolve_latest_session_id(ctx, sender)
        or str(ctx.session)
    )
    checkout_ref = ctx.storage.get(f"payment_request:by_checkout:{tx_id}") or {}
    if isinstance(checkout_ref, dict):
        expected_user = str(checkout_ref.get("user_address") or "")
        if expected_user and expected_user != sender:
            log_outbound(ctx, "RejectPayment", sender, f"reason=checkout_owner_mismatch tx_id={tx_id}")
            await ctx.send(sender, RejectPayment(reason="Checkout session does not belong to this user."))
            return
        expected_amount = checkout_ref.get("amount_cents")
        expected_currency = str(checkout_ref.get("currency") or "").lower()
        actual_amount = result.get("amount_total")
        actual_currency = str(result.get("currency") or "").lower()
        if isinstance(expected_amount, int) and actual_amount is not None and int(actual_amount) != expected_amount:
            log_outbound(ctx, "RejectPayment", sender, f"reason=amount_mismatch tx_id={tx_id}")
            await ctx.send(sender, RejectPayment(reason="Stripe amount mismatch."))
            return
        if expected_currency and actual_currency and actual_currency != expected_currency:
            log_outbound(ctx, "RejectPayment", sender, f"reason=currency_mismatch tx_id={tx_id}")
            await ctx.send(sender, RejectPayment(reason="Stripe currency mismatch."))
            return
    log_state(ctx, "session_id_resolved", f"tx_id={tx_id} session={session_id} method=stripe")

    ctx.storage.set(f"payments:processed:{sender}:{tx_id}", True)
    ctx.storage.set(f"{sender}:{session_id}:verified_payment", True)
    ctx.storage.remove(f"payment_request:pending:{sender}:{session_id}")
    ctx.storage.remove(f"payment_request:by_checkout:{tx_id}")
    log_state(ctx, "payment_verified", f"user={sender} session={session_id} tx_id={tx_id} method=stripe")

    log_outbound(ctx, "CompletePayment", sender, f"tx_id={tx_id} method=stripe")
    await ctx.send(sender, CompletePayment(transaction_id=tx_id))

    await _resume_paid_fulfillment(ctx, sender=sender, session_id=session_id, tx_id=tx_id, replay=False)


async def _on_commit_fet(ctx: Context, sender: str, msg: CommitPayment) -> None:
    tx_id = str(getattr(msg, "transaction_id", "") or "")
    cancel_tx_id = tx_id or "missing_transaction_id"
    currency = str(getattr(msg.funds, "currency", "") or "")

    if currency != "FET":
        log_outbound(ctx, "CancelPayment", sender, f"reason=unsupported currency={currency}")
        await ctx.send(
            sender,
            CancelPayment(transaction_id=cancel_tx_id, reason=f"Unsupported FET currency: {currency}"),
        )
        return

    if not tx_id:
        log_outbound(ctx, "CancelPayment", sender, "reason=missing_tx_id method=fet_direct")
        await ctx.send(sender, CancelPayment(transaction_id=cancel_tx_id, reason="Missing transaction_id"))
        return

    buyer_fet_wallet = extract_buyer_fet_wallet(msg.metadata)
    if not buyer_fet_wallet:
        log_outbound(ctx, "CancelPayment", sender, "reason=missing_buyer_fet_wallet")
        await ctx.send(
            sender,
            CancelPayment(transaction_id=cancel_tx_id, reason="Missing buyer_fet_wallet in metadata"),
        )
        return

    use_testnet = _use_testnet()
    network_name = "stable-testnet" if use_testnet else "mainnet"
    ctx.logger.info(
        f"[payment] fet verifying tx tx_id={tx_id} network={network_name} "
        f"buyer={buyer_fet_wallet} recipient={_agent_wallet.address()} amount={msg.funds.amount}"
    )
    try:
        verified = await asyncio.to_thread(
            verify_fet_payment_to_agent,
            transaction_id=tx_id,
            expected_amount_fet=str(msg.funds.amount),
            sender_fet_address=buyer_fet_wallet,
            recipient_agent_wallet=_agent_wallet,
            logger=ctx.logger,
            use_mainnet=not use_testnet,
        )
    except Exception as e:
        ctx.logger.exception(f"[payment] fet verify error: {e}")
        verified = False

    ctx.logger.info(f"[payment] fet verify result tx_id={tx_id} verified={verified}")
    if not verified:
        log_outbound(ctx, "CancelPayment", sender, f"reason=verify_failed tx_id={tx_id}")
        await ctx.send(
            sender,
            CancelPayment(transaction_id=cancel_tx_id, reason="Payment verification failed"),
        )
        return

    session_id = _resolve_latest_session_id(ctx, sender)
    try:
        ctx.storage.set(f"payments:processed:{sender}:{tx_id}", True)
        ctx.storage.set(f"{sender}:{session_id}:verified_payment", True)
        ctx.storage.remove(f"payment_request:pending:{sender}:{session_id}")
        log_state(ctx, "payment_verified", f"user={sender} session={session_id} tx_id={tx_id} method=fet_direct")
    except Exception as e:
        ctx.logger.warning(f"[payment] failed to persist verified state: {e}")

    log_outbound(ctx, "CompletePayment", sender, f"tx_id={tx_id} method=fet_direct")
    await ctx.send(sender, CompletePayment(transaction_id=tx_id))

    await _resume_paid_fulfillment(ctx, sender=sender, session_id=session_id, tx_id=tx_id, replay=False)


@payment_proto.on_message(CommitPayment)
async def on_commit(ctx: Context, sender: str, msg: CommitPayment) -> None:
    tx_id = str(getattr(msg, "transaction_id", "") or "")
    method = str(getattr(msg.funds, "payment_method", "") or "")
    currency = str(getattr(msg.funds, "currency", "") or "")
    amount = str(getattr(msg.funds, "amount", "") or "")
    log_inbound(
        ctx, "CommitPayment", sender,
        f"tx_id={tx_id or 'MISSING'} method={method} currency={currency} amount={amount}",
    )

    if _agent_wallet is None:
        ctx.logger.error("[payment] agent wallet not set; cannot verify")
        log_outbound(ctx, "CancelPayment", sender, "reason=server_wallet_missing")
        await ctx.send(
            sender,
            CancelPayment(transaction_id=tx_id or "missing_transaction_id", reason="Server wallet not configured"),
        )
        return

    # Idempotency BEFORE method dispatch — duplicate commits are method-agnostic.
    if tx_id:
        processed_key = f"payments:processed:{sender}:{tx_id}"
        try:
            if ctx.storage.get(processed_key):
                if method == "stripe":
                    stripe_result = verify_checkout_session_paid(tx_id)
                    stripe_meta = stripe_result.get("metadata") or {}
                    session_id = (
                        metadata_value(stripe_meta, "session_id")
                        or _lookup_session_id_by_checkout(ctx, tx_id)
                        or _resolve_latest_session_id(ctx, sender)
                    )
                elif method == "fet_direct":
                    session_id = _resolve_latest_session_id(ctx, sender)
                else:
                    session_id = str(ctx.session)
                fulfilled = bool(ctx.storage.get(_fulfilled_key(sender, session_id, tx_id)))
                log_state(
                    ctx,
                    "duplicate_commit",
                    f"tx_id={tx_id} method={method} session={session_id} fulfilled={fulfilled}",
                )
                log_outbound(ctx, "CompletePayment", sender, f"tx_id={tx_id} idempotent_replay=true")
                await ctx.send(sender, CompletePayment(transaction_id=tx_id))
                if method in {"stripe", "fet_direct"} and not fulfilled:
                    await _resume_paid_fulfillment(
                        ctx,
                        sender=sender,
                        session_id=session_id,
                        tx_id=tx_id,
                        replay=True,
                    )
                return
        except Exception:
            pass

    if method == "stripe" and _env_true("ENABLE_STRIPE_PAYMENTS", True):
        await _on_commit_stripe(ctx, sender, msg)
    elif method == "fet_direct" and _env_true("ENABLE_FET_PAYMENTS", True):
        await _on_commit_fet(ctx, sender, msg)
    else:
        # Unknown / disabled method. Use RejectPayment as the generic seller failure.
        log_outbound(ctx, "RejectPayment", sender, f"reason=unsupported_method method={method}")
        await ctx.send(
            sender,
            RejectPayment(reason=f"Unsupported or disabled payment method: {method}"),
        )


@payment_proto.on_message(RejectPayment)
async def on_reject(ctx: Context, sender: str, msg: RejectPayment) -> None:
    reason = str(getattr(msg, "reason", "") or "no reason")
    log_inbound(ctx, "RejectPayment", sender, f"reason={reason[:120]}")

    session_id = _resolve_latest_session_id(ctx, sender)
    try:
        ctx.storage.remove(f"payment_request:pending:{sender}:{session_id}")
        log_state(ctx, "pending_request_cleared", f"user={sender} session={session_id}")
    except Exception:
        pass

    reply = _chat(
        "Payment was not completed. Reply if you want to try again and I'll send a new payment request."
    )
    log_outbound(ctx, "ChatMessage", sender, "payment_reject_ack")
    await ctx.send(sender, reply)
```

---

## File 4 — `protocols/chat_proto.py`

Owns the Chat Protocol and the chat handler. Imports `request_payment_from_user` and the logging helpers from `payment_proto`. No Stripe SDK, no `cosmpy`, no payment-protocol message types here.

```python
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from uuid import uuid4

from openai import OpenAI
from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)

from protocols.payment_proto import (
    log_inbound,
    log_outbound,
    log_state,
    request_payment_from_user,
)

chat_proto = Protocol(spec=chat_protocol_spec)

ASI_ONE_BASE_URL = "https://api.asi1.ai/v1"
ASI_ONE_MODEL = os.getenv("ASI_ONE_MODEL", "asi1").strip() or "asi1"
ASI_ONE_SYSTEM_PROMPT = (
    os.getenv("ASI_ONE_SYSTEM_PROMPT")
    or "You are a helpful, precise assistant. Answer the user's request directly."
)

_asi_client: OpenAI | None = None


def _get_asi_client() -> OpenAI:
    global _asi_client
    if _asi_client is None:
        _asi_client = OpenAI(
            api_key=os.getenv("ASI_ONE_API_KEY"),
            base_url=ASI_ONE_BASE_URL,
        )
    return _asi_client


def _call_asi_one(prompt: str, *, session_id: str) -> str:
    response = _get_asi_client().chat.completions.create(
        model=ASI_ONE_MODEL,
        messages=[
            {"role": "system", "content": ASI_ONE_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=1000,
        extra_headers={"x-session-id": session_id} if session_id else None,
    )
    return response.choices[0].message.content or "(no response)"


def create_text_chat(text: str) -> ChatMessage:
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=[TextContent(type="text", text=text)],
    )


def _user_text(msg: ChatMessage) -> str:
    parts = [c.text for c in msg.content if isinstance(c, TextContent)]
    return " ".join(parts).strip()


def _is_paid_request(text: str) -> bool:
    """Replace this with the agent's real 'paid action requested' detector."""
    return bool(text)


@chat_proto.on_message(ChatMessage)
async def handle_chat(ctx: Context, sender: str, msg: ChatMessage) -> None:
    log_inbound(ctx, "ChatMessage", sender, f"msg_id={msg.msg_id}")

    ack = ChatAcknowledgement(
        timestamp=datetime.now(timezone.utc),
        acknowledged_msg_id=msg.msg_id,
    )
    log_outbound(ctx, "ChatAcknowledgement", sender, f"acknowledged_msg_id={msg.msg_id}")
    await ctx.send(sender, ack)

    text = _user_text(msg)
    session_id = str(ctx.session)
    try:
        already_paid = bool(ctx.storage.get(f"{sender}:{session_id}:verified_payment"))
    except Exception:
        already_paid = False

    ctx.logger.info(f"[inbound] text from {sender}: {text[:120]!r} already_paid={already_paid}")

    if already_paid:
        # Reuse the verified flag for follow-up messages in the same session.
        # Trade-off: one paid session keeps serving until ctx.session changes.
        # If you require pay-per-message, remove the flag here and persist a
        # counter instead.
        await run_paid_action(ctx, sender, session_id, text=text)
        return

    if _is_paid_request(text):
        await request_payment_from_user(
            ctx, sender,
            description="Pay with card or FET to run this request.",
            text=text,
        )
        notice = create_text_chat(
            "Once payment completes (card or FET), I'll reply here with your result."
        )
        log_outbound(ctx, "ChatMessage", sender, "awaiting_payment_notice")
        await ctx.send(sender, notice)
        return

    greet = create_text_chat("How can I help?")
    log_outbound(ctx, "ChatMessage", sender, "default_greeting")
    await ctx.send(sender, greet)


@chat_proto.on_message(ChatAcknowledgement)
async def handle_chat_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
    log_inbound(ctx, "ChatAcknowledgement", sender, f"acknowledged_msg_id={msg.acknowledged_msg_id}")


async def run_paid_action(
    ctx: Context,
    user_address: str,
    session_id: str,
    text: str | None = None,
) -> None:
    """Run the paid action and reply on the chat protocol.

    Default implementation: route the prompt to ASI:One. Replace with image
    gen, video gen, a deterministic API call, etc., when the use case is not
    a chat completion.
    """
    prompt = (text or "").strip() or "Say hello."
    try:
        reply_text = await asyncio.to_thread(_call_asi_one, prompt, session_id=session_id)
    except Exception as e:
        ctx.logger.exception(f"[payment] ASI:One call failed: {e}")
        reply_text = "Sorry — the assistant call failed. Please try again."
    reply = create_text_chat(reply_text)
    log_outbound(ctx, "ChatMessage", user_address, "paid_action_result")
    await ctx.send(user_address, reply)
```

---

## Default LLM (ASI:One)

`run_paid_action(...)` ships as a working ASI:One call out of the box. ASI:One is the **default** LLM for any agent scaffolded by this skill: it is OpenAI Chat Completions API-compatible, so the standard `openai` Python SDK works unchanged with `base_url` pointed at `https://api.asi1.ai/v1`. The wiring is already shown inline in File 4 — this section explains when to deviate.

### Decision: which LLM to use

Walk the rules in order; pick the first match and stop.

1. **Does the paid action actually need an LLM?** If `run_paid_action` is image gen via a fixed endpoint, a deterministic API call, an echo, or anything non-LLM, replace the ASI:One body with the deterministic call and drop the `openai` import + `ASI_ONE_API_KEY`.
2. **Did the user explicitly name a different provider or model?** (e.g. `openai`, `gpt-4o`, `gpt-5`, `claude-3.5-sonnet`, `anthropic`, `gemini`, `groq`, `ollama`, `together`, `mistral`, etc.) — use exactly that and remove the ASI:One client. Treat `asi1`, `asi:one`, `asi-one`, `asione`, "ASI", or "ASI One" as an explicit confirmation of the default.
3. **Otherwise** — keep ASI:One. Do not require user opt-in.

When extending an existing project that already wires up a different provider (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `langchain_*`, an `llm.py` wrapper, etc.), reuse that wiring instead of adding ASI:One on top — but for a **new** scaffold, ASI:One is the answer.

Keep the LLM call confined to `protocols/chat_proto.py` (where `run_paid_action` lives). Do not import the OpenAI SDK from `payment_proto.py`, `stripe_payments/checkout.py`, or `fet_payments/ledger.py` — those files have a single concern each.

### Required env var (always)

`.env.example` always includes `ASI_ONE_API_KEY`. Validate it at agent startup the same way `AGENT_SEED` is validated so the agent fails fast instead of silently 401-ing on the first paid request:

```python
ASI_ONE_API_KEY = _required("ASI_ONE_API_KEY")
```

Do not add `OPENAI_API_KEY` (different provider) and never invent placeholder keys.

### Dependency (always)

Always add the official `openai` Python SDK alongside `uagents` / `uagents-core`. Route the install through the active package skill (`uv add openai`, `poetry add openai`, `pip install openai`, or document-only for `no-package`). Do not pin a version unless the project already pins others.

### Optional knobs (use only when the use case needs them)

ASI:One accepts every standard OpenAI parameter (`temperature`, `top_p`, `max_tokens`, `presence_penalty`, `frequency_penalty`, `stream`) plus two extras exposed via the OpenAI SDK's escape hatches:

- **Web search** — `extra_body={"web_search": True}` to let the model browse, `False` to force it off. Omit otherwise.
- **Agentic session persistence** — `extra_headers={"x-session-id": session_id}` to keep multi-turn agentic state across calls. Use a stable string per chat session (the chat `session_id` already passed into `run_paid_action` is a natural fit).

### Streaming

ASI:One occasionally emits chunks with no `choices` or empty `delta.content`. If streaming, guard every chunk:

```python
for chunk in response:
    if (
        getattr(chunk, "choices", None)
        and chunk.choices
        and getattr(chunk.choices[0], "delta", None)
        and chunk.choices[0].delta.content
    ):
        partial = chunk.choices[0].delta.content
```

For a paid-action reply, batch the streamed pieces into a single `ChatMessage` (or send incremental `ChatMessage`s with new `msg_id`s — the chat protocol does not support partial updates of an existing message).

### Response shape

- `response.choices[0].message.content` — main reply text. Always read this for plain chat.
- `response.usage` — token usage (`prompt_tokens`, `completion_tokens`, `total_tokens`).
- `response.model` — model name echoed back.
- ASI:One-specific fields (read defensively with `getattr`, may be absent): `executable_data` (Agentverse agent calls / tool manifests), `intermediate_steps` (multi-step reasoning trace), `thought` (model reasoning).

### Hard rules for ASI:One

- Never hardcode the API key — always `os.getenv("ASI_ONE_API_KEY")`.
- Never log the API key, full streamed text, or `intermediate_steps` / `thought` payloads — they may carry tool arguments or sensitive context. Log lengths only, consistent with the Directional Logging Contract above.
- `base_url` is always exactly `https://api.asi1.ai/v1`. Do not call `https://api.openai.com` while routing to ASI:One.
- Do not silently fall back to another provider if ASI:One is unreachable — surface the error and let the user decide. Silent fallback hides outages and bills the wrong account.
- Keep the LLM call inside `protocols/chat_proto.py` (in `run_paid_action`). Do not put it in `payment_proto.py` or in either SDK-wrapper module.

---

## File 5 — `agent.py`

Tiny entrypoint. Loads env, creates the Agent, sets the wallet hook, includes both protocols. No business logic.

```python
from __future__ import annotations

import os

from dotenv import load_dotenv
from uagents import Agent
from uagents.setup import fund_agent_if_low

load_dotenv()

# Import protocol modules AFTER load_dotenv so module-level os.getenv reads .env values.
from protocols.chat_proto import chat_proto
from protocols.payment_proto import payment_proto, set_agent_wallet


def _required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


# Validate critical env vars up front so the agent fails fast instead of
# 401-ing on the first paid request or breaking after Stripe has already
# charged a card.
AGENT_SEED = _required("AGENT_SEED")
ASI_ONE_API_KEY = _required("ASI_ONE_API_KEY")

# Development default: testnet. Switch to "mainnet" only when the user explicitly
# opts into a production deploy. testnet is what makes the agent register against
# the Fetch.ai testnet Almanac contract instead of mainnet.
AGENT_NETWORK = (os.getenv("AGENT_NETWORK") or "testnet").strip().lower()

agent = Agent(
    name=os.getenv("AGENT_NAME", "payment-agent"),
    seed=AGENT_SEED,
    port=int(os.getenv("AGENT_PORT", "8000")),
    endpoint=os.getenv("AGENT_ENDPOINT") or None,
    mailbox=os.getenv("AGENT_MAILBOX", "true").strip().lower() == "true",
    agentverse=os.getenv("AGENTVERSE_URL", "https://agentverse.ai"),
    network=AGENT_NETWORK,
)

# REQUIRED on testnet: top up the agent wallet so Almanac registration succeeds.
# Without this call uagents logs:
#   WARNING: [uagents.registration] I do not have enough funds to register on Almanac contract
#   WARNING: [uagents.registration] To enable contract registration, send funds to wallet address: fetch1...
# fund_agent_if_low is a no-op once the wallet already has enough testnet FET, so it's safe on every restart.
# On mainnet the user is expected to fund the wallet themselves; do not call it then.
if AGENT_NETWORK == "testnet":
    fund_agent_if_low(str(agent.wallet.address()))

set_agent_wallet(agent.wallet)

agent.include(chat_proto, publish_manifest=True)
agent.include(payment_proto, publish_manifest=True)


if __name__ == "__main__":
    agent.run()
```

---

## File 6 — `.env.example`

List every variable the code reads. Nothing else. Use Stripe **test keys** by default; switch to live keys only after the user confirms they have completed Stripe's go-live checklist. Use `FET_USE_TESTNET=true` (stable-testnet `atestfet`) until end-to-end ledger verification has been confirmed.

```dotenv
# Agent identity
AGENT_NAME=payment-agent
AGENT_SEED=replace-with-a-strong-secret-seed
AGENT_PORT=8000
AGENT_ENDPOINT=
AGENT_MAILBOX=true
AGENTVERSE_URL=https://agentverse.ai
# Almanac registration network. "testnet" auto-funds the wallet via fund_agent_if_low
# and registers against the Fetch.ai testnet contract. Switch to "mainnet" only when
# going to production (the user is then responsible for funding the wallet).
AGENT_NETWORK=testnet

# --- Stripe (server-side keys; use TEST keys until going live) ---
ENABLE_STRIPE_PAYMENTS=true
STRIPE_SECRET_KEY=sk_test_...
STRIPE_PUBLISHABLE_KEY=pk_test_...
# Optional: pin a pre-created Stripe Price; otherwise inline price_data is used
STRIPE_PRICE_ID=
STRIPE_AMOUNT_CENTS=100
STRIPE_CURRENCY=usd
STRIPE_PRODUCT_NAME=Agent service
STRIPE_SUCCESS_URL=https://agentverse.ai/payment-success
STRIPE_CHECKOUT_EXPIRES_SECONDS=1800

# --- FET on-chain payments ---
ENABLE_FET_PAYMENTS=true
FET_AMOUNT_FET=0.001
# "true" -> stable-testnet (atestfet); "false" -> mainnet (afet)
FET_USE_TESTNET=true
FET_LEDGER_QUERY_TIMEOUT_SECONDS=20

# --- Shared payment UX knobs ---
CHECKOUT_DEADLINE_SECONDS=300
PAYMENT_REQUEST_PENDING_TTL_SECONDS=1800
PAYMENT_REQUEST_RESEND_MIN_INTERVAL_SECONDS=60

# --- Default LLM (ASI:One, OpenAI-compatible) ---
# ASI:One is the default LLM used by run_paid_action. Required at startup —
# the agent fails fast if missing. See "Default LLM (ASI:One)" above for the
# decision tree if you want to swap in a different provider.
ASI_ONE_API_KEY=
# Optional overrides:
ASI_ONE_MODEL=asi1
ASI_ONE_SYSTEM_PROMPT=You are a helpful, precise assistant. Answer the user's request directly.
```

Rules for env vars:

- Only list variables the code actually reads.
- Never commit a real `.env`. If `.gitignore` exists, ensure `.env` is in it; if it doesn't, skip — do not create one.
- `ENABLE_STRIPE_PAYMENTS=false` makes the agent FET-only at runtime; `ENABLE_FET_PAYMENTS=false` makes it Stripe-only. Disabling both refuses every paid request.

---

## Stripe API Version Note (Dahlia, 2026-03-25)

There are **two unrelated `ui_mode` fields** in this skill — keep them straight or you will introduce a regression while trying to fix one:

| layer | where | values | controlled by |
| --- | --- | --- | --- |
| Stripe API parameter | `stripe.checkout.Session.create(ui_mode=...)` in `stripe_payments/checkout.py` | `embedded_page` (Dahlia+) or `embedded` (legacy accounts) | Stripe |
| Agentverse UI metadata | `RequestPayment.metadata["stripe"]["ui_mode"]` in `protocols/payment_proto.py` | always the literal `"embedded"` | Agentverse frontend |

Stripe's 2026-03-25 ("Dahlia") release renamed the Checkout Session `ui_mode` enum on the API side: `embedded` -> `embedded_page`, `hosted` -> `hosted_page`, `custom` -> `elements`. Sending the old values to a Dahlia-or-later account fails with a 400 like "The ui_mode value `embedded` is no longer supported. Use `embedded_page` instead." The helper in File 1 tries `embedded_page` first and falls back to `embedded` only if Stripe rejects the new value (account pinned to a pre-Dahlia API version). Each attempt uses its own idempotency key so Stripe doesn't replay a cached 400.

The Agentverse UI metadata stays `"embedded"` regardless of what Stripe's API accepted. That exact literal in `metadata["stripe"]["ui_mode"]` is the contract the Agentverse frontend uses to pick the embedded-widget renderer; it is not a passthrough of Stripe's API enum.

Frontends mounting the embedded checkout should now call `stripe.createEmbeddedCheckoutPage({ fetchClientSecret })` instead of `stripe.initEmbeddedCheckout(...)`.

---

## Funds + Metadata Shape (reference)

`RequestPayment.accepted_funds` (multi-method offer):

```json
[
  { "currency": "USD", "amount": "1.00",  "payment_method": "stripe" },
  { "currency": "FET", "amount": "0.001", "payment_method": "fet_direct" }
]
```

`RequestPayment.metadata`:

```json
{
  "agent": "payment-agent",
  "stripe": {
    "ui_mode": "embedded",
    "publishable_key": "pk_test_...",
    "client_secret": "cs_test_...",
    "checkout_session_id": "cs_test_...",
    "amount_cents": 100,
    "currency": "usd"
  },
  "fet_network": "stable-testnet",
  "mainnet": "false",
  "provider_agent_wallet": "fetch1agentwallet..."
}
```

`CommitPayment` shapes (one of):

```json
// Stripe path
{
  "transaction_id": "cs_test_...",
  "funds": { "currency": "USD", "amount": "1.00", "payment_method": "stripe" }
}

// FET path
{
  "transaction_id": "<tx hash on the chosen Fetch.ai network>",
  "funds": { "currency": "FET", "amount": "0.001", "payment_method": "fet_direct" },
  "metadata": { "buyer_fet_wallet": "fetch1buyer..." }
}
```

`buyer_fet_address` is also accepted as an alias for `buyer_fet_wallet`.

---

## Dynamic Pricing

Two valid patterns. **Always compute the amount on the seller side.** Never accept an amount from the chat message, the UI, or the buyer's `CommitPayment`.

- **Server-computed `amount_cents` (per request)** — pass `amount_cents_override=...` to `create_embedded_checkout_session`. The helper bakes `amount_cents` into the `idempotency_key` so re-attempts at a different price never collide with a prior session. With dynamic Stripe pricing also assert the retrieved Checkout Session matches the seller-intended amount: reject if `result["amount_total"] != expected_cents` or `result["currency"] != expected_currency.lower()`.
- **Pre-created Stripe Prices** — set `STRIPE_PRICE_ID`; the helper switches to `line_items=[{"price": ..., "quantity": 1}]` automatically.

For FET, set `FET_AMOUNT_FET` (or compute it server-side and pass it through your own builder). The verifier already uses `msg.funds.amount` to derive the expected micro-FET, which prevents under-payment via a tampered commit.

---

## Hard Rules

- Resolve package manager via the **Package Skill Precedence** block before any setup/run instructions; never mix package-manager commands in one solution.
- Default `Agent(...)` to `network="testnet"` for development (driven by `AGENT_NETWORK=testnet` in `.env.example`); switch to `"mainnet"` only when the user explicitly opts into a production deploy.
- On `network="testnet"`, always call `fund_agent_if_low(str(agent.wallet.address()))` immediately after `Agent(...)` so the wallet has enough testnet FET for Almanac registration. This is what suppresses the `[uagents.registration] I do not have enough funds to register on Almanac contract` warning loop. On mainnet, do NOT call it — the operator funds the wallet themselves.
- Register both protocols on the agent: `agent.include(chat_proto, publish_manifest=True)` and `agent.include(payment_proto, publish_manifest=True)`.
- Always send a single `RequestPayment` whose `accepted_funds` lists every enabled method. Do not send one `RequestPayment` per method.
- Dispatch `CommitPayment` by `msg.funds.payment_method`. Stripe path verifies via `stripe.checkout.Session.retrieve(...).payment_status == "paid"`; FET path verifies via `verify_fet_payment_to_agent` wrapped in `asyncio.to_thread`. Never `await` the synchronous FET verifier directly.
- Stripe failure reply: `RejectPayment(reason=...)`. FET failure reply: `CancelPayment(transaction_id=..., reason=...)`. Unknown / disabled method: `RejectPayment(reason=...)`. Keep the convention; it matches the Agentverse UI behavior.
- Use the chain `transaction_id` (Checkout Session ID for Stripe, tx hash for FET) as the idempotency key (`payments:processed:<sender>:<tx_id>`), and track a fulfillment marker (`payment:fulfilled:<sender>:<session>:<tx_id>`) so duplicate commits can safely resume `run_paid_action(...)` if the first post-verify fulfillment was interrupted.
- Compute price on the **seller side**. Never derive `unit_amount` or `expected_amount_fet` from chat input or `CommitPayment`. For dynamic Stripe pricing, also verify `amount_total` + `currency` on retrieve.
- The seller agent's wallet must come from `agent.wallet`, deterministically derived from `AGENT_SEED`. Never hardcode addresses. `provider_agent_wallet` in metadata must be `str(agent.wallet.address())`.
- Reject FET commits without `buyer_fet_wallet` (or `buyer_fet_address`) in metadata, or whose `currency != "FET"`.
- One pending `RequestPayment` per `(user_address, ctx.session)`. Do not spawn a new Checkout Session per chat message; reuse the pending one until TTL or completion.
- Persist the sender's latest paid-request session (`payment_request:latest_session:<user>`) and use it as a fallback when commit callbacks arrive with session drift/replay context.
- Pass an `idempotency_key` when calling `stripe.checkout.Session.create`.
- Every handler starts with `log_inbound(...)`; every `ctx.send(...)` is preceded by `log_outbound(...)`; notable storage transitions use `log_state(...)`. Domain logs use `[payment] stripe ...` or `[payment] fet ...` so the dispatch path is visible.
- Never log `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `client_secret`, `AGENT_SEED`, raw private keys, full signed transactions, full Stripe responses, full `msg.metadata` dumps, or card data.

## Forbidden

- Naming the local Stripe folder `stripe/` — it shadows the SDK. Use `stripe_payments/`. Same applies to `cosmpy/` or `fetchai/` as top-level packages.
- Calling the Stripe SDK from `chat_proto.py`, `payment_proto.py`, or `agent.py`. The SDK lives **only** in `stripe_payments/checkout.py`.
- Calling `cosmpy` from `chat_proto.py`, `payment_proto.py`, or `agent.py`. `cosmpy` lives **only** in `fet_payments/ledger.py`.
- Replying to `CommitPayment` with raw text as the payment-protocol reply. Always send `CompletePayment` / `RejectPayment` / `CancelPayment` first; optional chat text may follow as a separate message.
- Fulfilling the paid action before the per-method verifier returns `verified=True` (Stripe) or `True` (FET).
- Subscriptions / recurring prices in `mode="payment"` (will fail). For subscriptions use `mode="subscription"`, only after confirming with the user.
- Sending raw card details to the agent — the user pays inside Stripe's embedded Checkout.
- Calling `verify_fet_payment_to_agent(tx_hash=..., agent_wallet=..., expected_amount=...)`. The correct kwargs are `transaction_id`, `recipient_agent_wallet`, `expected_amount_fet`. Wrong kwargs silently fail.
- Importing `chat_proto` from `payment_proto` at module top level — use the `_chat(...)` helper inside functions to avoid circular imports.
- Skipping verification when `cosmpy` or `stripe` is missing or times out. Disabled / unconfigured methods must be removed from `accepted_funds` upfront, not "succeed" silently in `on_commit`.

---

## Wrapping Patterns

- **New agent from scratch** → create the six files above plus `.env`, then implement `_is_paid_request` and `run_paid_action` to suit the use case.
- **Existing chat-only uAgent** → drop `stripe_payments/checkout.py` and `fet_payments/ledger.py` in unchanged, add `protocols/payment_proto.py`, call `set_agent_wallet(agent.wallet)` in `agent.py` right after the `Agent(...)` construction, then `agent.include(payment_proto, publish_manifest=True)`. In your existing chat handler, gate the paid branch with `ctx.storage.get(f"{sender}:{session_id}:verified_payment")` and call `request_payment_from_user(...)` when missing.
- **Existing single-method agent (Stripe-only or FET-only)** → keep the relevant SDK file, add the missing one (`stripe_payments/checkout.py` or `fet_payments/ledger.py`), replace `protocols/payment_proto.py` with the combined version above, and update `.env.example` to include both blocks. The `accepted_funds` builder will skip whichever method is disabled or unconfigured at runtime.
- **Agent that should be FET-only or Stripe-only at runtime** → keep this skill's code unchanged; toggle `ENABLE_STRIPE_PAYMENTS` / `ENABLE_FET_PAYMENTS` in `.env`. `_accepted_methods()` will drop the disabled side from `accepted_funds`.

---

## Sample Chat (target UX)

```
User:  generate me an image of a sunset
Agent: [ChatAcknowledgement]
Agent: [RequestPayment with accepted_funds=[stripe USD, fet_direct FET]
        and metadata containing both metadata.stripe and metadata.provider_agent_wallet]
       "Once payment completes (card or FET), I'll reply here with your result."

# Path A — buyer picks card:
UI:    (Stripe embedded card form rendered from client_secret + publishable_key)
User:  pays with test card 4242 4242 4242 4242
UI:    sends CommitPayment(transaction_id=<checkout_session_id>, funds.payment_method="stripe")
Agent: [CompletePayment]
Agent: "Here's your image of a sunset: <url>"   # run_paid_action fired automatically

# Path B — buyer picks FET:
UI:    Asks the user to broadcast a FET transfer to provider_agent_wallet on stable-testnet
User:  signs + broadcasts the transfer in their wallet
UI:    sends CommitPayment(transaction_id=<tx hash>,
                           funds.payment_method="fet_direct",
                           metadata={"buyer_fet_wallet": "fetch1..."})
Agent: [CompletePayment]
Agent: "Here's your image of a sunset: <url>"   # run_paid_action fired automatically
```

The chat handler decides *when* to call `request_payment_from_user(...)`. The payment handlers decide *how* to verify and reply per method. Keep these concerns in their own files.

---

## Testing Hints

- Run with both methods enabled first to confirm `accepted_funds` advertises both. Then flip `ENABLE_STRIPE_PAYMENTS=false` and confirm the offer drops to FET only; flip back, set `ENABLE_FET_PAYMENTS=false`, confirm the offer drops to Stripe only.
- **Stripe** — use a Stripe sandbox / test-mode account before live keys. Test cards:
  - Success: `4242 4242 4242 4242`
  - Decline: `4000 0000 0000 0002` — confirms the `RejectPayment` path
  - 3DS challenge: `4000 0027 6000 3184` — confirms the agent only sends `CompletePayment` once `payment_status == "paid"`
- **FET** — use stable-testnet first (`FET_USE_TESTNET=true`). Fund the buyer wallet from the Fetch.ai testnet faucet, broadcast to `provider_agent_wallet`, and send `CommitPayment` with the resulting tx hash.
- **Idempotency** — re-send the same `CommitPayment` for both methods:
  - if fulfillment already completed, second call should only emit `CompletePayment ... idempotent_replay=true`;
  - if fulfillment failed after verification, second call should resume `run_paid_action(...)` and log `post_payment_fulfilled ... replay=true`.
- **Cross-contamination** — send a Stripe `CommitPayment` after disabling Stripe (or FET commit after disabling FET). The reply must be `RejectPayment(reason="Unsupported or disabled payment method: ...")`.
- **Reject path** — send `RejectPayment` from the buyer side and confirm pending state is cleared (`[state] pending_request_cleared`) and the user receives the `payment_reject_ack` chat reply.

### Log Verification Checklist

Run the agent locally and confirm CLI output contains, in order, for a card flow:

- `[inbound] ChatMessage from <user>` then `[outbound] ChatAcknowledgement -> <user>`.
- `[payment] stripe checkout created cs_id=cs_test_... amount_cents=... currency=... ui_mode=...`.
- `[outbound] RequestPayment -> <user> methods=stripe,fet_direct ref=... recipient=fetch1...`.
- `[state] pending_request_saved user=<user> session=<session> ref=... methods=stripe,fet_direct`.
- `[inbound] CommitPayment from <user> tx_id=cs_test_... method=stripe currency=USD amount=...`.
- `[payment] stripe verify tx_id=cs_test_... verified=true status=paid amount_total=... currency=...`.
- `[state] session_id_resolved tx_id=cs_test_... session=<session> method=stripe` (fallback chain ran successfully).
- `[state] payment_verified ... method=stripe` then `[outbound] CompletePayment -> <user> tx_id=cs_test_... method=stripe`.
- `[outbound] ChatMessage -> <user> paid_action_result` after the protocol reply (auto-fulfilled by `run_paid_action`).

For a FET flow, the same checklist with the FET-specific lines:

- `[payment] fet verifying tx tx_id=... network=stable-testnet buyer=fetch1... recipient=fetch1... amount=...`.
- `[payment] fet verify result tx_id=... verified=true`.
- `[state] payment_verified ... method=fet_direct` then `[outbound] CompletePayment -> <user> tx_id=... method=fet_direct`.
- Failure path emits `[outbound] CancelPayment -> <user> reason=...` (FET) or `[outbound] RejectPayment -> <user> reason=...` (Stripe / unsupported).
- Duplicate replay path emits `[state] duplicate_commit ... fulfilled=<true|false>` + `idempotent_replay=true`; when `fulfilled=false` it must also log `[state] post_payment_fulfilled ... replay=true` after resuming fulfillment.

Confirm that NO logs contain `sk_...`, `pk_...`, `client_secret`, full `msg.metadata` dumps, `AGENT_SEED`, raw private keys, or full signed transactions.

---

## Further Reading (optional)

These are not required to implement the agent — everything the agent needs is above. Consult only if a specific edge case surfaces:

- `uagents_core.contrib.protocols.payment`: `Funds`, `RequestPayment`, `CommitPayment`, `CompletePayment`, `CancelPayment`, `RejectPayment`, `payment_protocol_spec`.
- Stripe Checkout Sessions API — `https://docs.stripe.com/api/checkout/sessions` and Stripe test cards — `https://docs.stripe.com/testing`.
- `cosmpy.aerial.client.LedgerClient` and `NetworkConfig.fetchai_mainnet()` / `NetworkConfig.fetchai_stable_testnet()`.
- Companion single-method skills (`stripe-payment-protocol`, `fet-payment-protocol`) when only one method is wanted.
- Companion `chat-protocol` skill for the underlying `ChatMessage` / `ChatAcknowledgement` lifecycle.
