"""The registry is the server-side truth every card selection and payment is checked against."""

from __future__ import annotations

import json
import time

from core import config
from core.models import Finding, JudgeRequest, Verdict
from core.registry import Registry, SlotLog
from tests.fake_storage import Lockstep


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
    row = reg.kv.get("verdict:aud000000001")
    reg.kv.set("verdict:aud000000001", {**row, "created_at": time.time() - 2 * config.CACHE_TTL_S})
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


def test_latest_verdict_skips_audits_that_graded_nothing():
    reg = Registry(":memory:")
    assert reg.latest_verdict("agent1qt") is None
    reg.save_verdict(verdict("aud-done"), "alice")
    empty = Verdict(audit_id="aud-empty", target_name="ShopBot", target_address="agent1qt", grade="F", score=0,
                    partial=True, partial_reason="nothing could be graded; no result from leak probe")
    reg.save_verdict(empty, "alice")  # e.g. stored by an older build before the Orchestrator refused to cache these
    stored = reg.latest_verdict("agent1qt")
    assert stored is not None and stored.verdict.audit_id == "aud-done"
    assert abs(time.time() - stored.audited_at) < 5 and not stored.verified


def test_a_failed_audit_can_be_retried_once_and_keeps_its_payment_ref():
    reg = Registry(":memory:")
    reg.create_pending("aud1", "alice", "{}")
    assert reg.claim_pending("aud1", "alice", "awaiting_payment", "running", payment_ref="0xpaid")
    assert reg.claim_pending("aud1", "alice", "running", "failed")
    assert reg.claim_pending("aud1", "alice", "failed", "running")
    assert not reg.claim_pending("aud1", "alice", "failed", "running")
    row = reg.get_pending("aud1", "alice")
    assert row["state"] == "running" and row["payment_ref"] == "0xpaid"
    assert reg.latest_pending("alice", "running")["audit_id"] == "aud1"


# --- overlapping invocations (hosted agents): tests/fake_storage.Lockstep interleaves two of them -----------------


def hosted(store) -> Registry:
    """A cold start, as every hosted invocation is: a fresh Registry over the agent's storage."""
    reg = Registry(store=store)
    reg.seed_allowlist()
    return reg


def test_a_cold_start_overlapping_a_new_plan_does_not_expire_it():
    """Live: a plan was 'expired' 20 s after it was made. A sweep tick's cold start rewrote the whole DB snapshot."""
    store = Lockstep()
    making_the_plan = hosted(store.shared)  # this invocation has started; the sweep's cold start lands mid-write
    store.overlap(lambda _: making_the_plan.create_pending("aud1", "alice", "{}"), lambda s: hosted(s).active_audits())
    assert hosted(store.plain).get_pending("aud1", "alice")["state"] == "awaiting_payment"


def test_a_cold_start_writes_nothing():
    store = Lockstep()
    hosted(store.plain)
    before = store.writes
    reg = hosted(store.plain)
    reg.active_audits(), reg.judge_due(time.time()), reg.latest("agent1qt"), reg.find_agent("ShopBot-Solid")
    assert store.writes == before


def test_overlapping_claims_start_one_audit():
    store = Lockstep()
    hosted(store.plain).create_pending("aud1", "alice", "{}")
    wins = store.overlap(*[lambda s: hosted(s).claim_pending("aud1", "alice", "awaiting_payment", "running", "free")] * 2)
    assert sorted(wins) == [False, True]
    assert hosted(store.plain).get_pending("aud1", "alice")["state"] == "running"


def test_overlapping_payments_with_one_tx_id_count_once():
    store = Lockstep()
    wins = store.overlap(lambda s: hosted(s).record_payment("0xabc", "alice", "aud1", "fet"),
                         lambda s: hosted(s).record_payment("0xabc", "bob", "aud2", "fet"))
    assert sorted(wins) == [False, True]


def test_overlapping_audits_are_all_active_and_popped_once():
    store = Lockstep()
    store.overlap(lambda s: hosted(s).add_active("aud1", "alice", "{}", "", False),
                  lambda s: hosted(s).add_active("aud2", "bob", "{}", "", False))
    assert {r["audit_id"] for r in hosted(store.plain).active_audits()} == {"aud1", "aud2"}
    popped = store.overlap(*[lambda s: hosted(s).pop_active("aud1")] * 2)
    assert sum(p is not None for p in popped) == 1
    assert [r["audit_id"] for r in hosted(store.plain).active_audits()] == ["aud2"]


def test_a_verdict_saved_during_a_cold_start_is_kept():
    store = Lockstep()
    hosted(store.plain)
    store.overlap(lambda s: hosted(s).save_verdict(verdict(), "alice"), lambda s: hosted(s).seed_allowlist())
    assert hosted(store.plain).latest("agent1qt").grade == "F"


def test_overlapping_probe_results_are_both_kept_and_each_counts_once():
    store = Lockstep()
    req = JudgeRequest(audit_id="aud1", target_name="T", target_address="agent1qt", expected_kinds=["leak", "payment"],
                       requester="agent1qorch")
    assert hosted(store.plain).judge_open("aud1", req.json(), time.time() + 60)
    store.overlap(lambda s: hosted(s).judge_add_result("aud1", "leak", '{"kind": "leak"}'),
                  lambda s: hosted(s).judge_add_result("aud1", "payment", '{"kind": "payment"}'))
    reg = hosted(store.plain)
    assert set(json.loads(reg.judge_get("aud1"))["results"]) == {"leak", "payment"}
    assert not reg.judge_add_result("aud1", "leak", '{"kind": "leak", "again": true}')  # the first one per probe wins
    assert not reg.judge_open("aud1", req.json(), time.time() + 60)  # a duplicate request changes nothing
    graded = store.overlap(*[lambda s: hosted(s).judge_pop("aud1")] * 2)
    assert sum(g is not None for g in graded) == 1
    assert reg.judge_get("aud1") is None and reg.judge_due(time.time() + 120) == []
    assert not reg.judge_add_result("aud1", "leak", '{"kind": "leak", "late": true}')  # graded: a late result is dropped


def test_a_consuming_reader_skips_a_slot_that_may_still_be_overwritten():
    store = Lockstep()
    log = SlotLog(store.plain, "in:run1")
    log.append("old")
    rec = store.plain.get("in:run1:0")
    store.plain.set("in:run1:0", {**rec, "at": time.time() - 5})
    log.append("fresh")
    assert [v for _, v in log.items(0, settled_s=1.0)] == ["old"]  # the fresh slot waits for the next step
    assert log.get(1, settled_s=1.0) is None and log.get(1) == "fresh"
    assert [v for _, v in log.items(0)] == ["old", "fresh"]
