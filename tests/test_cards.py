"""Every card builder produces a schema-valid payload under the size and depth limits."""

from __future__ import annotations

import json

import pytest

from core import cards, chat
from core.models import AttackCase, AuditPlan, Finding, ProbeSummary, Verdict
from tests.card_schema import validate

FINDING = Finding(
    case_id="pay-fake-hash", title="Delivers after a fake payment", category="payment", severity="critical",
    hard=True, evidence="CompletePayment tx=0xdead", fix="Verify the transaction on the ledger before delivering.",
)


def verdict(grade="F", partial=False, findings=(FINDING,)) -> Verdict:
    return Verdict(
        audit_id="aud000000001", target_name="ShopBot-Overcharge", target_address="agent1qx" + "a" * 30,
        grade=grade, score=20 if grade == "F" else 100, partial=partial, partial_reason="no result from leak probe" if partial else "",
        findings=list(findings),
        sections=(
            [ProbeSummary(kind="injection", passed=1, failed=1, skipped=0), ProbeSummary(kind="payment", passed=0, failed=2, skipped=1)]
            if findings
            else [ProbeSummary(kind="injection", passed=3, failed=0, skipped=0), ProbeSummary(kind="payment", passed=4, failed=0, skipped=0)]
        ),
        passed=1 if findings else 7, total=5 if findings else 7,
    )


def plan() -> AuditPlan:
    cases = [
        AttackCase(id=f"c{i}", category=cat, title="t", action="chat", rule_kind="soft", generated=i == 0)
        for i, cat in enumerate(["injection", "leak", "payment"])
    ]
    return AuditPlan(audit_id="aud000000001", target_name="ShopBot-Overcharge", target_address="agent1q", cases=cases,
                     rationale="Shop agent: try a 100% discount and a refund with no order.")


NOW = 1_800_000_000.0
FRESH_S = 3600


def lookup(age_s: float, **kw) -> cards.CardSpec:
    return cards.trust_lookup(verdict(**kw), NOW - age_s, fresh_s=FRESH_S, now=NOW, verified=True, last_verified=NOW - 120)


SPECS = {
    "trust_lookup": lookup(754),
    "trust_lookup_partial": lookup(754, grade="B", partial=True, findings=()),
    "trust_lookup_stale": lookup(6 * 3600 + 780),
    "audit_plan": cards.audit_plan(plan(), rail_label="FET testnet"),
    "scorecard_f": cards.scorecard(verdict()),
    "scorecard_a": cards.scorecard(verdict("A", findings=())),
    "scorecard_partial": cards.scorecard(verdict("B", partial=True, findings=())),
    "payment_link": cards.payment_link(plan(), "https://test.checkout.dodopayments.com/session/cks_0001", rail_label="Dodo Payments test mode"),
    "finding_detail": cards.finding_detail(FINDING, "aud000000001", "agent1q"),
    "evidence_overview": cards.evidence_overview(verdict()),
    "owner_claim_form": cards.owner_claim_form(),
    "owner_code_card": cards.owner_code_card("agent1q" + "c" * 30, "canary-verify-1a2b3c4d"),
    "notice": cards.notice("Audit could not finish", "ShopBot went silent.", "Check the agent is online and retry.", action="retry", audit_id="x"),
    "notice_no_action": cards.notice("Not allowed", "Agent not on the allowlist.", "Ask the owner to verify it first."),
}


@pytest.mark.parametrize("name", SPECS)
def test_card_is_schema_valid(name):
    spec = SPECS[name]
    assert validate(spec.kind, spec.payload) == []
    assert chat.card_is_valid(spec.kind, spec.payload)
    assert len(json.dumps(spec.payload).encode()) < chat.CARD_MAX_BYTES
    assert spec.summary and "\n" not in spec.summary


@pytest.mark.parametrize("name", SPECS)
def test_card_becomes_a_chat_message_with_string_metadata(name):
    spec = SPECS[name]
    msg = chat.card(*spec)
    cards_in = chat.extract_cards(msg)
    assert len(cards_in) == 1 and cards_in[0]["kind"] == spec.kind
    meta = next(c for c in msg.content if c.type == "metadata").metadata
    assert meta["card_protocol_version"] == "1" and all(isinstance(v, str) for v in meta.values())
    assert chat.extract_text(msg) == spec.summary


def test_scorecard_leads_with_grade_and_shows_quote_and_fix():
    spec = SPECS["scorecard_f"]
    children = spec.payload["root"]["children"]
    first = children[0]["children"][0]
    assert first == {"type": "heading", "value": "F", "level": 1}
    blob = json.dumps(spec.payload)
    assert "CompletePayment tx=0xdead" in blob and "Verify the transaction on the ledger" in blob
    assert "CompletePayment tx=0xdead" in spec.summary


def _badge_variants(node) -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "badge":
            out.append(node["variant"])
        for v in node.values():
            out += _badge_variants(v)
    elif isinstance(node, list):
        for v in node:
            out += _badge_variants(v)
    return out


def test_scorecard_badges_follow_the_visual_language():
    assert "warning" in _badge_variants(SPECS["scorecard_f"].payload)
    assert "success" in _badge_variants(SPECS["scorecard_a"].payload)
    assert "warning" not in _badge_variants(SPECS["scorecard_a"].payload)
    assert "info" in _badge_variants(SPECS["scorecard_partial"].payload)  # partial = not final


def test_each_interactive_card_has_one_primary_action():
    for name in ("trust_lookup", "trust_lookup_partial", "trust_lookup_stale", "finding_detail", "notice"):
        ctas = SPECS[name].payload["ctas"]
        assert sum(1 for c in ctas if c.get("primary")) == 1
    assert SPECS["audit_plan"].payload["approve_cta"]["primary"]


def test_plan_card_selection_carries_audit_id_only():
    sel = SPECS["audit_plan"].payload["approve_cta"]["selection"]
    assert sel == {"action": "pay_and_run", "audit_id": "aud000000001"}


def test_invalid_card_degrades_to_text():
    msg = chat.card("custom", {"nope": 1}, "fallback line")
    assert chat.extract_cards(msg) == [] and chat.extract_text(msg) == "fallback line"
    deep: dict = {"type": "section", "children": []}
    node = deep
    for _ in range(9):
        child: dict = {"type": "section", "children": []}
        node["children"].append(child)
        node = child
    assert not chat.card_is_valid("custom", {"root": deep})


def test_parse_selection_handles_json_and_prose():
    assert chat.parse_selection('{"action": "confirm", "audit_id": "aud000000001"}')["via"] == "json"
    p = chat.parse_selection("The user approved pay_and_run for audit_id aud000000001 (ShopBot).")
    assert p["action"] == "pay_and_run" and p["via"] == "prose"
    c = chat.parse_selection("Please cancel the audit 0123456789ab")
    assert c["action"] == "cancel" and c["audit_id"] == "0123456789ab"
    a = chat.parse_selection("verify agent agent1q" + "x" * 30)
    assert a["action"] == "verify" and a["address"].startswith("agent1q")
    assert chat.parse_selection("is shopbot safe?")["via"] == "none"


def _rows(spec: cards.CardSpec) -> dict[str, str]:
    return {r["label"]: r["value"] for r in spec.payload["summary_rows"]}


def test_fresh_lookup_shows_exact_age_and_audit_id():
    spec = SPECS["trust_lookup"]
    rows = _rows(spec)
    assert spec.payload["title"] == "ShopBot-Overcharge: grade F"
    assert rows["Grade"] == "F" and rows["Audit id"] == "aud000000001"
    assert rows["Audited"].startswith("12 min 34 s ago (") and "UTC" in rows["Audited"]
    assert rows["Freshness"] == "within my 1 h window"
    assert spec.summary.startswith("ShopBot-Overcharge graded F (20/100) in audit aud000000001, 12 min 34 s ago")
    assert "Free lookup" not in spec.summary


def test_partial_lookup_is_always_labelled_with_its_reason():
    spec = SPECS["trust_lookup_partial"]
    rows = _rows(spec)
    assert rows["Grade"] == "B (partial)" and rows["Partial verdict"] == "no result from leak probe"
    assert "graded B (partial: no result from leak probe)" in spec.summary
    stale = cards.historical_lookup(verdict("B", partial=True, findings=()), NOW - 2 * FRESH_S, fresh_s=FRESH_S, now=NOW)
    assert "B (partial: no result from leak probe)" in stale


def test_stale_verdict_is_history_not_the_current_grade():
    text = cards.historical_lookup(verdict(), NOW - (6 * 3600 + 780), fresh_s=FRESH_S, now=NOW)
    assert text.startswith("I have no current grade for ShopBot-Overcharge")
    assert "aud000000001" in text and "6 h 13 min old" in text and "older than my 1 h freshness window" in text
    assert "Historical result, for reference only: F (20/100)" in text
    assert "graded F" not in text and "scored F" not in text  # the hosted tester counts "graded X" as a current grade
    spec = SPECS["trust_lookup_stale"]  # handed a stale verdict anyway, the card still says history, not grade
    assert spec.payload["title"] == "ShopBot-Overcharge: last result (historical)"
    assert "Grade" not in _rows(spec) and _rows(spec)["Freshness"] == "older than my 1 h window"
    assert spec.summary.startswith("I have no current grade")


def test_freshness_window_edges():
    assert cards.is_fresh(NOW - FRESH_S, FRESH_S, NOW)
    assert not cards.is_fresh(NOW - FRESH_S - 1, FRESH_S, NOW)
    assert not cards.is_fresh(NOW + 60, FRESH_S, NOW)  # a timestamp from the future is not trusted as fresh
    assert cards.span(3600) == "1 h" and cards.span(240) == "4 min" and cards.span(80) == "1 min 20 s"
    assert cards.span(0) == "0 s" and cards.span(90061) == "1 d 1 h"


def test_plan_card_duration_comes_from_the_audit_deadline():
    from core import config

    rows = _rows(SPECS["audit_plan"])
    assert rows["Takes"].startswith(f"up to {cards.span(config.AUDIT_TIMEOUT_S)}")
    assert "60 seconds" not in json.dumps(SPECS["audit_plan"].payload)
