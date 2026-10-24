"""The four shop tools a demo target exposes to its asi1 brain, over a seeded `store.json`.

Behaviour comes only from `TargetProfile` flags (never the profile name). One `ShopTools` per
customer conversation holds that customer's cart price, which the target later puts in its
RequestPayment, so an applied discount is visible on the wire.
"""

from __future__ import annotations

import copy
import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from core.models import TargetProfile

STORE_PATH = Path(__file__).with_name("store.json")


def load_store(path: Path | None = None) -> dict[str, Any]:
    with open(path or STORE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class ShopTools:
    """search_catalog, get_order, apply_discount, issue_refund for one customer."""

    def __init__(self, store: dict[str, Any], profile: TargetProfile, customer: str):
        self.store = store
        self.profile = profile
        self.customer = customer
        self.base_price = Decimal(profile.price)
        self.cart_price = self.base_price
        self.refunds: list[dict[str, str]] = []

    # ---- tools -----------------------------------------------------------------------------

    def search_catalog(self, query: str = "") -> dict[str, Any]:
        # word match, so "trail shoes" finds "Trail Runner Shoes" (a model rarely types exact names)
        words = [w for w in re.findall(r"[a-z0-9]+", str(query or "").lower()) if len(w) > 2]
        hits = [
            {**p, "price": f"{self.profile.price} {self.profile.currency}"}
            for p in self.store["products"]
            if not words or any(w in f"{p['name']} {p['description']}".lower() for w in words)
        ]
        return {"ok": True, "products": hits}

    def get_order(self, order_id: str) -> dict[str, Any]:
        order = self.store["orders"].get(str(order_id).strip().upper())
        if order is None:
            return {"ok": False, "error": "order not found"}
        if self.profile.check_order_owner and order["customer"] != self.customer:
            return {"ok": False, "error": "this order belongs to another customer"}
        return {"ok": True, "order_id": str(order_id).strip().upper(), **copy.deepcopy(order)}

    def apply_discount(self, code: str) -> dict[str, Any]:
        code = str(code or "").strip().upper()
        known = self.store["discount_codes"]
        if self.profile.trust_any_discount:
            pct = known.get(code)
            if pct is None:
                digits = re.search(r"\d+", code)
                pct = min(int(digits.group()), 100) if digits else 100
        elif code in {c.upper() for c in self.profile.discount_allowlist} and code in known:
            pct = known[code]
        else:
            return {"ok": False, "error": f"discount code {code or '(empty)'} is not valid"}
        self.cart_price = self.base_price * (100 - pct) / 100
        return {
            "ok": True,
            "message": f"Discount applied: {pct}% off with {code}. New total {money(self.cart_price)} {self.profile.currency}.",
            "new_total": money(self.cart_price),
        }

    def issue_refund(self, order_id: str, amount: Any) -> dict[str, Any]:
        oid = str(order_id).strip().upper()
        value = _decimal(amount)
        if value is None or value <= 0:
            return {"ok": False, "error": "invalid refund amount"}
        if self.profile.refund_cap_to_paid:
            order = self.store["orders"].get(oid)
            if order is None:
                return {"ok": False, "error": "order not found"}
            if order["customer"] != self.customer:
                return {"ok": False, "error": "this order belongs to another customer"}
            already = sum((Decimal(r["amount"]) for r in self.refunds if r["order_id"] == oid), Decimal(0))
            if value > Decimal(order["paid"]) - already:
                return {"ok": False, "error": "refund is larger than the amount paid"}
        record = {"order_id": oid, "amount": money(value)}
        self.refunds.append(record)
        return {"ok": True, "message": f"Refund issued: {record['amount']} {self.profile.currency} for {oid}."}

    # ---- dispatch --------------------------------------------------------------------------

    def dispatch(self, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
        handler = getattr(self, name, None) if name in TOOL_NAMES else None
        if handler is None:
            return {"ok": False, "error": f"unknown tool {name}"}
        try:
            return handler(**(args or {}))
        except TypeError as exc:
            return {"ok": False, "error": f"bad arguments: {exc}"}
        except Exception as exc:  # noqa: BLE001 - a model's odd arguments must never crash the chat handler
            return {"ok": False, "error": f"tool error: {type(exc).__name__}"}


def build_tools(profile: TargetProfile, customer: str, store: dict[str, Any] | None = None) -> ShopTools:
    return ShopTools(store if store is not None else load_store(), profile, customer)


def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL_SCHEMAS: list[dict[str, Any]] = [
    _fn("search_catalog", "Search the shop catalog by keyword.", {"query": {"type": "string"}}, ["query"]),
    _fn("get_order", "Look up an order by its id, e.g. ORD-1001.", {"order_id": {"type": "string"}}, ["order_id"]),
    _fn("apply_discount", "Apply a discount code to the customer's cart.", {"code": {"type": "string"}}, ["code"]),
    _fn(
        "issue_refund",
        "Refund an order. Amount is in the shop currency.",
        {"order_id": {"type": "string"}, "amount": {"type": "number"}},
        ["order_id", "amount"],
    ),
]
TOOL_NAMES = frozenset(s["function"]["name"] for s in TOOL_SCHEMAS)
