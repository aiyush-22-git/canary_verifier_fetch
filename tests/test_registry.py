"""The registry is the server-side truth every card selection and payment is checked against."""

from __future__ import annotations

import time

from core import config
from core.models import Finding, Verdict
from core.registry import Registry


def verdict(audit_id="aud000000001", address="agent1qt", grade="F") -> Verdict:
    f = Finding(case_id="leak-canary", title="Leaks its prompt", category="leak", severity="critical", hard=True,
                evidence="secret=CANARY-X", fix="Keep secrets out of the prompt.")
    return Verdict(audit_id=audit_id, target_name="ShopBot", target_address=address, grade=grade, score=10, findings=[f])


def test_demo_targets_are_preallowed():
    reg = Registry(":memory:")
    reg.seed_allowlist()
    for t in config.DEMO_TARGETS.values():
        assert reg.is_allowed(t.resolve_address()) and reg.canary_for(t.resolve_address()) == t.canary
    assert reg.find_agent("shopbot-solid") is not None


def test_pending_is_private_to_the_requester_and_single_use():
    reg = Registry(":memory:")
    reg.create_pending("aud1", "alice", "{}")
    assert reg.get_pending("aud1", "mallory") is None  # someone else's plan can't be paid or cancelled
    assert reg.claim_pending("aud1", "alice", "awaiting_payment", "running")
    assert not reg.claim_pending("aud1", "alice", "awaiting_payment", "running")  # second claimer loses


def test_payment_ledger_rejects_replays():
    reg = Registry(":memory:")
    assert reg.record_payment("0xabc", "alice", "aud1", "fet_direct")
    assert not reg.record_payment("0xabc", "bob", "aud2", "fet_direct")
    assert reg.payment_seen("0xabc") and not reg.payment_seen("0xdef")


def test_latest_respects_the_24h_cache_window():
    reg = Registry(":memory:")
    reg.save_verdict(verdict(), "alice")
    assert reg.latest("agent1qt", max_age_s=config.CACHE_TTL_S).grade == "F"
    reg._run("UPDATE audits SET created_at=?", (time.time() - 2 * config.CACHE_TTL_S,))
    assert reg.latest("agent1qt", max_age_s=config.CACHE_TTL_S) is None
    assert reg.latest("agent1qt") is not None  # free lookups still show an older score, with its age


def test_owner_code_needs_the_right_user_and_the_code_in_the_profile():
    reg = Registry(":memory:")
    code = reg.issue_owner_code("agent1qowned", "alice", "MyAgent")
    assert not reg.confirm_owner_code("agent1qowned", "mallory", f"desc {code}")
    assert not reg.confirm_owner_code("agent1qowned", "alice", "no code here")
    assert not reg.is_allowed("agent1qowned")
    assert reg.confirm_owner_code("agent1qowned", "alice", f"My agent. {code}")
    assert reg.is_allowed("agent1qowned") and len(reg.verified_agents()) == 1
    assert not reg.confirm_owner_code("agent1qowned", "alice", f"{code}")  # one-time
