"""Dodo Payments rail: session creation and server-side verification against a fake Dodo API."""

from __future__ import annotations

import pytest
from uagents_core.contrib.protocols.payment import CommitPayment, Funds

from core import config, payments
from tests.fake_dodo import FakeDodo


@pytest.fixture
def dodo(monkeypatch) -> FakeDodo:
    fake = FakeDodo()
    monkeypatch.setattr(config, "PAYMENT_RAIL", "dodo")
    monkeypatch.setattr(config, "DODO_API_KEY", "dodo_test_key")
    monkeypatch.setattr(config, "DODO_PRODUCT_ID", "pdt_audit")
    monkeypatch.setattr(config, "AUDIT_PRICE_USD", "1.00")
    monkeypatch.setattr(payments, "_dodo_http", fake)
    return fake


def new_session(dodo: FakeDodo, audit_id="aud000000001") -> str:
    made = payments.create_dodo_session(audit_id, "agent1qbuyer")
    assert made is not None
    return made[0]


def test_session_creation_carries_the_audit_in_metadata(dodo):
    sid, url = payments.create_dodo_session("aud000000001", "agent1qbuyer")
    assert url.endswith(sid) and dodo.sessions[sid]["metadata"] == {"audit_id": "aud000000001", "requester": "agent1qbuyer"}


def test_dodo_down_means_no_session_and_no_crash(dodo):
    dodo.fail = True
    assert payments.create_dodo_session("aud000000001", "agent1qbuyer") is None


def test_rail_is_off_until_key_and_product_are_set(monkeypatch):
    monkeypatch.setattr(config, "PAYMENT_RAIL", "dodo")
    monkeypatch.setattr(config, "DODO_API_KEY", "")
    assert not payments.enabled()
    monkeypatch.setattr(config, "DODO_API_KEY", "k")
    monkeypatch.setattr(config, "DODO_PRODUCT_ID", "")
    assert not payments.enabled()


def test_unpaid_checkout_is_not_verified(dodo):
    v = payments.verify_dodo(new_session(dodo), "aud000000001")
    assert not v.ok and "not paid yet" in v.reason


def test_paid_checkout_verifies_and_returns_the_payment_id(dodo):
    sid = new_session(dodo)
    pid = dodo.pay(sid)
    v = payments.verify_dodo(sid, "aud000000001")
    assert v.ok and v.tx_id == pid and v.method == "dodo"


@pytest.mark.parametrize(
    ("kw", "reason", "kind"),
    [
        ({"amount": 99}, "below the audit price", "refused"),
        ({"currency": "EUR"}, "unexpected currency", "refused"),
        ({"status": "failed"}, "payment failed", "failed"),
        ({"status": "cancelled"}, "payment cancelled", "failed"),
        ({"status": "processing"}, "not paid yet", "pending"),
        ({"status": "requires_customer_action"}, "not paid yet", "pending"),
        ({"metadata": {"audit_id": "someone-elses"}}, "different audit", "refused"),
    ],
)
def test_bad_payments_are_refused(dodo, kw, reason, kind):
    sid = new_session(dodo)
    dodo.pay(sid, **kw)
    v = payments.verify_dodo(sid, "aud000000001")
    assert not v.ok and reason in v.reason and v.kind == kind


def test_unpaid_and_unreachable_say_what_to_do(dodo):
    sid = new_session(dodo)
    assert payments.verify_dodo(sid, "aud000000001").kind == "unpaid"
    dodo.fail = True
    assert payments.verify_dodo(sid, "aud000000001").kind == "unreachable"


def test_session_keeps_the_buyer_in_usd_without_discount_codes(dodo, monkeypatch):
    calls = []

    def spy(method, path, body=None):
        calls.append(body)
        return dodo(method, path, body)

    monkeypatch.setattr(payments, "_dodo_http", spy)
    payments.create_dodo_session("aud000000001", "agent1qbuyer")
    body = calls[0]
    assert body["billing_currency"] == "USD"
    assert body["feature_flags"] == {"allow_currency_selection": False, "allow_discount_code": False}


class _Resp:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self) -> bytes:
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def test_http_client_is_stdlib_with_a_timeout_and_bearer_auth(monkeypatch):
    """The hosted runtime has no httpx/requests guarantee: _dodo_http must be urllib, JSON in and out, bounded."""
    import json
    import urllib.request

    seen = {}

    def fake_urlopen(req, timeout):
        seen.update(url=req.full_url, method=req.get_method(), timeout=timeout, body=json.loads(req.data),
                    auth=req.get_header("Authorization"), ctype=req.get_header("Content-type"), ua=req.get_header("User-agent"))
        return _Resp(b'{"session_id": "cks_1", "checkout_url": "https://test.checkout.dodopayments.com/session/cks_1"}')

    monkeypatch.setattr(config, "DODO_API_KEY", "dodo_test_key")
    monkeypatch.setattr(config, "DODO_ENV", "test_mode")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = payments._dodo_http("POST", "/checkouts", {"product_cart": []})
    assert out["session_id"] == "cks_1"
    assert seen["url"] == "https://test.dodopayments.com/checkouts" and seen["method"] == "POST"
    assert seen["auth"] == "Bearer dodo_test_key" and seen["ctype"] == "application/json"
    assert 0 < seen["timeout"] <= 15 and seen["ua"] and "urllib" not in seen["ua"].lower()
    assert seen["body"] == {"product_cart": []}


def test_a_payment_made_for_another_checkout_is_refused(dodo):
    mine, other = new_session(dodo, "aud000000001"), new_session(dodo, "aud000000002")
    dodo.pay(mine, bound_to=other)  # the payment record says it belongs to a different checkout
    v = payments.verify_dodo(mine, "aud000000001")
    assert not v.ok and "different checkout" in v.reason


def test_tax_on_top_of_the_price_is_fine(dodo):
    sid = new_session(dodo)
    dodo.pay(sid, amount=118)
    assert payments.verify_dodo(sid, "aud000000001").ok


def test_dodo_lookup_failure_fails_closed(dodo):
    sid = new_session(dodo)
    dodo.pay(sid)
    dodo.fail = True
    v = payments.verify_dodo(sid, "aud000000001")
    assert not v.ok and "couldn't reach Dodo" in v.reason and v.kind == "unreachable"


def commit(tx: str, method: str = "dodo") -> CommitPayment:
    return CommitPayment(funds=Funds(currency="USD", amount="1.00", payment_method=method), recipient="r", transaction_id=tx)


def test_commit_must_name_the_session_we_issued(dodo):
    sid = new_session(dodo)
    pid = dodo.pay(sid)
    assert payments.verify_commit(commit(sid), "agent1qbuyer", "aud000000001", "w", dodo_session=sid).ok
    for tx in (pid, "cks_9999", "0xdeadbeef"):  # a payment id, someone else's session, a made-up hash
        v = payments.verify_commit(commit(tx), "agent1qbuyer", "aud000000001", "w", dodo_session=sid)
        assert not v.ok and "issued for this audit" in v.reason
    assert not payments.verify_commit(commit(sid, "fet_direct"), "agent1qbuyer", "aud000000001", "w", dodo_session=sid).ok
    assert not payments.verify_commit(commit(sid), "agent1qbuyer", "aud000000001", "w", dodo_session="").ok
