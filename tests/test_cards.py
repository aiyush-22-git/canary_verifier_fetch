"""Every card builder produces a schema-valid payload under the size and depth limits."""

from __future__ import annotations

import json
import time

import pytest

from core import cards, chat
from core.models import AttackCase, AuditPlan, Finding, ProbeSummary, RegistryEntry, Verdict
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


ENTRY = RegistryEntry(
    target_name="ShopBot-Leaky", target_address="agent1q" + "b" * 30, grade="F", score=12, partial=False,
    top_findings=[FINDING], audited_at=time.time() - 3600, verified=True, last_verified=time.time() - 120,
)

SPECS = {
    "trust_lookup": cards.trust_lookup(ENTRY),
    "audit_plan": cards.audit_plan(plan(), rail_label="FET testnet"),
    "scorecard_f": cards.scorecard(verdict()),
    "scorecard_a": cards.scorecard(verdict("A", findings=())),
    "scorecard_partial": cards.scorecard(verdict("B", partial=True, findings=())),
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
    for name in ("trust_lookup", "finding_detail", "notice"):
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
