"""Pure card payload builders. No I/O, no uAgents: each function returns a CardSpec that
`core.chat.send_card(ctx, to, *spec)` can send.

One visual language: success badge = passed, warning badge = finding, info badge = in progress.
The grade letter leads every scorecard, every finding shows its quote and a fix, and every card
has exactly one primary action. Every spec carries a one-line text summary because an invalid
card silently falls back to plain text.

Selections are plain JSON the Orchestrator validates against server-side state; they are
identifiers, never authority.
"""

from __future__ import annotations

import time
from typing import Any, NamedTuple

from core.models import AuditPlan, Finding, RegistryEntry, Verdict

PASS_GRADES = ("A", "B")
QUOTE_MAX = 220


class CardSpec(NamedTuple):
    kind: str
    payload: dict[str, Any]
    summary: str


def _ago(ts: float) -> str:
    secs = max(0, int(time.time() - ts))
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if secs >= size:
            return f"{secs // size}{unit} ago"
    return "just now"


def _clip(s: str, n: int = QUOTE_MAX) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _variant(grade: str) -> str:
    return "success" if grade in PASS_GRADES else "warning"


def _grade_line(grade: str, partial: bool) -> str:
    return f"{grade} (partial)" if partial else grade


# --- 1. trust lookup (detail) -------------------------------------------------------------


def trust_lookup(entry: RegistryEntry) -> CardSpec:
    rows = [
        {"label": "Grade", "value": _grade_line(entry.grade, entry.partial)},
        {"label": "Score", "value": f"{entry.score}/100"},
        {"label": "Last audited", "value": _ago(entry.audited_at)},
    ]
    if entry.verified:
        rows.append({"label": "Owner verified", "value": f"yes, re-tested {_ago(entry.last_verified)}"})
    for i, f in enumerate(entry.top_findings[:3], 1):
        rows.append({"label": f"Finding {i}: {f.title}", "value": f'"{_clip(f.evidence, 120)}"'})
    if not entry.top_findings:
        rows.append({"label": "Findings", "value": "none"})
    payload = {
        "title": f"{entry.target_name}: grade {entry.grade}",
        "summary_rows": rows,
        "ctas": [
            {
                "label": "Run a fresh audit",
                "selection": {"action": "run_fresh", "address": entry.target_address},
                "primary": True,
            }
        ],
    }
    return CardSpec(
        "detail", payload,
        f"{entry.target_name} scored {entry.grade} ({entry.score}/100), audited {_ago(entry.audited_at)}. Free lookup.",
    )


# --- 2. audit plan (review) -----------------------------------------------------------------


def audit_plan(plan: AuditPlan, *, rail_label: str) -> CardSpec:
    kinds = sorted({c.category for c in plan.cases})
    generated = sum(1 for c in plan.cases if c.generated)
    tests = f"{len(plan.cases)} live tests across {', '.join(kinds)}"
    if generated:
        tests += f" ({generated} written for this agent)"
    free = rail_label == "free"
    rows = [
        {"label": "Target", "value": plan.target_name},
        {"label": "Tests", "value": tests},
        {"label": "Takes", "value": "about 60 seconds"},
        {"label": "Price", "value": "Free" if free else f"{plan.price_usd} USD ({rail_label}{', test funds' if 'test' in rail_label else ''})"},
    ]
    if plan.rationale:
        rows.insert(2, {"label": "Why these tests", "value": _clip(plan.rationale, 140)})
    payload = {
        "title": f"Audit {plan.target_name}",
        "summary_rows": rows,
        "approve_cta": {
            "label": "Run the audit" if free else "Pay and run",
            "selection": {"action": "pay_and_run", "audit_id": plan.audit_id},
            "primary": True,
        },
        "reject_cta": {"label": "Cancel", "selection": {"action": "cancel", "audit_id": plan.audit_id}},
    }
    text = (f"Audit plan for {plan.target_name}: {len(plan.cases)} tests, free. Run it, or cancel." if free
            else f"Audit plan for {plan.target_name}: {len(plan.cases)} tests, {plan.price_usd} USD. Pay and run, or cancel.")
    return CardSpec("review", payload, text)


# --- 2b. payment link (detail): Dodo hosted checkout -----------------------------------------------------


def payment_link(plan: AuditPlan, url: str, *, rail_label: str) -> CardSpec:
    payload = {
        "title": f"Pay for the {plan.target_name} audit",
        "summary_rows": [
            {"label": "Amount", "value": f"{plan.price_usd} USD ({rail_label})"},
            {"label": "Step 1", "value": "Open the secure checkout link from my previous message and pay"},
            {"label": "Step 2", "value": "Come back and press the button. I confirm the payment with the provider before any test runs"},
            *([{"label": "Test mode", "value": "No real money moves: pay with a Dodo test card"}] if "test" in rail_label else []),
        ],
        "ctas": [
            {
                "label": "I've paid, start the audit",
                "selection": {"action": "check_payment", "audit_id": plan.audit_id},
                "primary": True,
            },
            {"label": "Cancel", "selection": {"action": "cancel", "audit_id": plan.audit_id}},
        ],
    }
    return CardSpec("detail", payload, f"Pay {plan.price_usd} USD here: {url} then press 'I've paid'.")


# --- 3. progress is plain text (core.chat.send_text), see progress_line() ------------------------


def progress_line(kind: str, headline: str) -> str:
    return f"[{kind}] {headline}"


# --- 4. scorecard (custom) -------------------------------------------------------------------------


def _finding_item(f: Finding, audit_id: str) -> dict[str, Any]:
    return {
        "children": [
            {"type": "badge", "label": f"{f.severity}{' · hard fail' if f.hard else ''}", "variant": "warning"},
            {"type": "heading", "value": f.title, "level": 3},
            {"type": "text", "value": f'"{_clip(f.evidence)}"', "style": "emphasis"},
            {"type": "text", "value": f"Fix: {_clip(f.fix, 160)}", "style": "muted"},
        ],
        "action": {"selection": {"action": "open_finding", "audit_id": audit_id, "case_id": f.case_id}},
    }


def scorecard(v: Verdict) -> CardSpec:
    probe_rows = [
        {
            "type": "group",
            "direction": "row",
            "gap": 8,
            "children": [
                {"type": "badge", "label": s.kind, "variant": "warning" if s.failed else "success"},
                {"type": "text", "value": f"{s.passed} passed · {s.failed} failed" + (f" · {s.skipped} skipped" if s.skipped else "")},
            ],
        }
        for s in v.sections
    ]
    children: list[dict[str, Any]] = [
        {
            "type": "group",
            "direction": "row",
            "gap": 12,
            "children": [
                {"type": "heading", "value": v.grade, "level": 1},
                {"type": "badge", "label": "partial" if v.partial else f"{v.score}/100", "variant": "info" if v.partial else _variant(v.grade)},
            ],
        },
        {"type": "text", "value": f"{v.passed} of {v.total} checks passed", "style": "muted"},
    ]
    if v.partial:
        children.append({"type": "text", "value": f"Partial verdict: {v.partial_reason}", "style": "muted"})
    children.append({"type": "divider"})
    children += probe_rows
    top = v.findings[:3]
    if top:
        children.append({"type": "divider"})
        children.append({"type": "heading", "value": "Top findings", "level": 2})
        children.append({"type": "list", "items": [_finding_item(f, v.audit_id) for f in top]})
    else:
        children.append({"type": "badge", "label": "No findings", "variant": "success"})
    children.append(
        {
            "type": "button",
            "label": "View evidence",
            "primary": True,
            "action": {"selection": {"action": "view_evidence", "audit_id": v.audit_id}},
        }
    )
    if v.trace:
        children.append(
            {
                "type": "button",
                "label": "View telemetry",
                "action": {"selection": {"action": "view_telemetry", "audit_id": v.audit_id}},
            }
        )
    payload = {"root": {"type": "section", "title": v.target_name, "subtitle": "Canary trust audit", "children": children}}
    lead = f"{v.target_name} graded {v.grade}" + (" (partial)" if v.partial else "") + f": {v.passed}/{v.total} checks passed."
    if top:
        lead += f' Top finding: {top[0].title} - "{_clip(top[0].evidence, 90)}"'
    return CardSpec("custom", payload, lead)


# --- 5. finding detail (detail) ------------------------------------------------------------------------


def finding_detail(f: Finding, audit_id: str, target_address: str) -> CardSpec:
    payload = {
        "title": f.title,
        "summary_rows": [
            {"label": "Severity", "value": f"{f.severity}{' (hard fail: caps the grade at F)' if f.hard else ''}"},
            {"label": "Area", "value": f.category},
            {"label": "Evidence", "value": f'"{_clip(f.evidence, 400)}"'},
            {"label": "Fix", "value": f.fix or "See the Fetch Chat and Payment Protocol guides."},
            {"label": "Found by", "value": "exact-match rule" if f.source == "rule" else "asi1 rubric grader"},
        ],
        "ctas": [
            {
                "label": "Re-test after fix",
                "selection": {"action": "retest", "audit_id": audit_id, "address": target_address},
                "primary": True,
            },
            {"label": "Back to scorecard", "selection": {"action": "view_evidence", "audit_id": audit_id}},
        ],
    }
    return CardSpec("detail", payload, f'{f.title} ({f.severity}): "{_clip(f.evidence, 120)}" Fix: {_clip(f.fix, 100)}')


def evidence_overview(v: Verdict) -> CardSpec:
    """'View evidence': one finding per row; pick one to open its detail."""
    items = [
        {
            "id": f.case_id,
            "title": f.title,
            "subtitle": f'"{_clip(f.evidence, 100)}"',
            "badges": [{"label": f.severity, "variant": "warning"}],
            "secondary_text": "hard fail" if f.hard else "finding",
            "primary_cta": {
                "label": "Open",
                "selection": {"action": "open_finding", "audit_id": v.audit_id, "case_id": f.case_id},
            },
        }
        for f in v.findings
    ]
    payload = {"title": f"{v.target_name}: evidence", "subtitle": f"{len(items)} finding(s)", "items": items}
    return CardSpec("carousel", payload, f"{len(items)} finding(s) for {v.target_name}. Open one to see the quote and the fix.")


# --- 6. owner verification (form) ---------------------------------------------------------------------------


def owner_claim_form() -> CardSpec:
    payload = {
        "title": "Verify you own an agent",
        "fields": [
            {"name": "address", "kind": "text", "label": "Agent address (agent1q...)", "required": True},
        ],
        "submit_cta": {"label": "Get my verification code", "selection": {"action": "claim_agent"}},
    }
    return CardSpec("form", payload, "Send the address of the agent you own and I will give you a one-time code.")


def owner_code_card(address: str, code: str) -> CardSpec:
    payload = {
        "title": "Add this code to your agent",
        "summary_rows": [
            {"label": "Agent", "value": address},
            {"label": "One-time code", "value": code},
            {"label": "Where", "value": "Paste it into the agent's description on its Agentverse profile"},
            {"label": "Valid for", "value": "30 minutes"},
        ],
        "approve_cta": {"label": "Verify", "selection": {"action": "verify", "address": address}, "primary": True},
        "reject_cta": {"label": "Cancel", "selection": {"action": "cancel_verify", "address": address}},
    }
    return CardSpec("review", payload, f"Add the code {code} to {address[:16]}... on Agentverse, then press Verify.")


# --- failure paths: say what happened and what to do next --------------------------------------------------------


def notice(title: str, what_happened: str, next_step: str, *, action: str | None = None, cta_label: str = "Try again", **selection: str) -> CardSpec:
    if action:
        ctas = [{"label": cta_label, "selection": {"action": action, **selection}, "primary": True}]
    else:
        ctas = [{"label": "Got it", "selection": {"action": "dismiss"}, "primary": True}]
    payload: dict[str, Any] = {
        "title": title,
        "summary_rows": [
            {"label": "What happened", "value": _clip(what_happened, 300)},
            {"label": "What to do next", "value": _clip(next_step, 300)},
        ],
        "ctas": ctas,
    }
    return CardSpec("detail", payload, f"{title}. {what_happened} Next: {next_step}")


# --- 7. telemetry report (plain text, chunked): the wire record behind a grade ------------------------------------


def telemetry_report(v: Verdict, *, chunk: int = 3200) -> list[str]:
    """Readable chunks of the per-case wire telemetry in `v.trace`, so anyone can check a grade against the agents' own logs."""
    import json

    try:
        data = json.loads(v.trace or "{}")
    except json.JSONDecodeError:
        data = {}
    cases = data.get("cases") or []
    if not cases:
        return []
    head = (
        f"TELEMETRY for audit {v.audit_id}: {data.get('target', v.target_name)} ({data.get('target_address', v.target_address)})\n"
        f"{len(cases)} tests ran live: {data.get('rule_graded', 0)} decided by exact-match rules, {data.get('asi1_graded', 0)} by the asi1 grader. "
        "Every 'sent' and 'target replied' line is copied from the wire, not generated.\n"
        f"To check it yourself, open the Logs of Canary-Orchestrator, the Canary-Probe-* agents and {v.target_name} on Agentverse and "
        f"search for this audit's traffic around {time.strftime('%H:%M:%S UTC', time.gmtime(v.generated_at))}."
    )
    blocks = []
    for i, c in enumerate(cases, 1):
        lines = [
            f"{i}. [{c['outcome']}] {c['title']}  ({c['probe']} probe, attack={c['attack']}, rule={c['rule']}, "
            f"{c['latency_ms'] / 1000:.1f}s, decided by: {c['decided_by']})"
        ]
        lines += [f"   sent: \"{s}\"" for s in c["sent"]]
        lines += [f"   target replied: \"{s}\"" for s in c["received"]] or ["   target replied: (nothing)"]
        if c["events"]:
            lines.append("   protocol events: " + "; ".join(c["events"]))
        if c["evidence"]:
            lines.append(f"   evidence quoted: \"{c['evidence']}\"")
        blocks.append("\n".join(lines))
    out, cur = [], head
    for b in blocks:
        if len(cur) + len(b) > chunk:
            out.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + b
    out.append(cur)
    return out
