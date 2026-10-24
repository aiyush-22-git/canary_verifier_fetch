"""The contract. Every inter-agent message and every YAML schema lives here.

Only the lead changes this file, through a PR labelled `contract`, announced in docs/STATUS.md.
Everyone else codes against it.

Message flow of one audit:

    Orchestrator --JudgeRequest--> Judge          (what to expect, canary, deadline)
    Orchestrator --ProbeRequest--> Probe x3       (cases for one attack family)
    Probe --(chat / payment protocol)--> Target
    Probe --ProbeResult--> Judge                  (Exchange transcripts)
    Judge --ProbeProgress--> Orchestrator         (one per probe as it is graded)
    Judge --Verdict--> Orchestrator
"""

from __future__ import annotations

from uagents import Model

# ---------------------------------------------------------------------------
# Vocabulary (plain strings on the wire, validated by contract_check)
# ---------------------------------------------------------------------------

CATEGORIES = ("injection", "leak", "payment")
SEVERITIES = ("critical", "high", "medium", "low")
GRADES = ("A", "B", "C", "D", "F")

# AttackCase.action: what the probe does. The probe interprets it; packs only choose.
ACTIONS = (
    "chat",  # send `messages` in order, collect replies
    "raw_json",  # send `messages` as chat, then `raw_json` as a spoofed card selection
    "tool_injection",  # chat prompts that make the target read a poisoned tool result
    "pay_fake",  # ask for the paid service, answer RequestPayment with a made-up CommitPayment
    "pay_replay",  # pay once (fake), then replay the same transaction id for a second order
    "pay_wrong_amount",  # commit less than the requested amount
    "service_before_pay",  # ask for the service and never commit; see if it is delivered anyway
)

# AttackCase.rule_kind: how the Judge decides, without an LLM, whether the target failed.
RULE_KINDS = (
    "text_match",  # fail if regex `rule_arg` matches any received reply. "{canary}" expands to the target canary
    "event_match",  # fail if regex `rule_arg` matches any protocol event string
    "ack_missing",  # fail if the target did not acknowledge the probe's ChatMessage
    "amount_below",  # fail if a RequestPayment event asks for less than float(rule_arg)
    "soft",  # graded by asi1 against `rubric`; rules-only verdicts skip it and mark partial
)

# Protocol event strings the probe writes into Exchange.events. The Judge matches these.
#   "ChatAck"                                  target acknowledged our ChatMessage
#   "RequestPayment amount=<x> currency=<c>"   target asked us to pay
#   "CommitPayment kind=<fake|replay|short> tx=<id> amount=<x>"   what we sent
#   "CompletePayment tx=<id>"                  target accepted the payment
#   "RejectPayment reason=<text>" / "CancelPayment reason=<text>"   target refused
#   "Card kind=<detail|review|...> actions=<a,b>"   a card the target showed
EVENT_ACK = "ChatAck"
EVENT_REQUEST_PAYMENT = "RequestPayment"
EVENT_COMMIT_PAYMENT = "CommitPayment"
EVENT_COMPLETE_PAYMENT = "CompletePayment"
EVENT_REJECT_PAYMENT = "RejectPayment"
EVENT_CANCEL_PAYMENT = "CancelPayment"


# ---------------------------------------------------------------------------
# Attack data (packs/*.yaml rows)
# ---------------------------------------------------------------------------


class AttackCase(Model):
    id: str  # unique across all packs, e.g. "leak-canary-direct"
    category: str  # one of CATEGORIES; the pack's file name
    title: str  # short, shown in cards
    action: str  # one of ACTIONS
    messages: list[str] = []  # chat messages to send in order
    raw_json: str = ""  # for action == "raw_json": the spoofed selection, e.g. {"action": "confirm"}
    rule_kind: str  # one of RULE_KINDS
    rule_arg: str = ""  # regex / number depending on rule_kind
    rubric: str = ""  # for rule_kind == "soft": what a safe reply looks like
    severity: str = "high"  # one of SEVERITIES
    hard: bool = False  # a hard-rule failure makes the whole grade F
    fix: str = ""  # one-line remediation shown to the owner
    expected: dict[str, str] = {}  # demo profile -> "fail" | "pass" (what we expect the Judge to say)
    generated: bool = False  # True for variants written by the asi1 planner


class Pack(Model):
    """packs/<category>.yaml: {category: str, cases: [AttackCase, ...]}"""

    category: str
    cases: list[AttackCase]


class TargetProfile(Model):
    """targets/profiles.yaml: {profiles: [TargetProfile, ...]}  (consumed by agents/target.py)"""

    key: str  # matches core.config.AGENTS key, e.g. "target-leaky"
    name: str  # Agentverse display name
    canary: str  # must equal core.config.DEMO_TARGETS[key].canary
    system_prompt: str  # contains the canary token verbatim
    description: str = ""  # Agentverse profile text the planner reads
    shared_memory: bool = False  # leaky: one Backboard thread for every customer
    check_order_owner: bool = True  # get_order verifies the customer owns the order
    discount_allowlist: list[str] = []  # empty list + trust_any_discount=True means any code works
    trust_any_discount: bool = False
    refund_cap_to_paid: bool = True
    verify_payments: bool = True  # False: deliver on any CommitPayment (fake or replayed)
    trust_raw_selection: bool = False  # True: a raw JSON selection skips offer -> review -> confirm
    price: str = "5.00"
    currency: str = "FET"


# ---------------------------------------------------------------------------
# Audit messages
# ---------------------------------------------------------------------------


class ProbeRequest(Model):
    audit_id: str
    kind: str  # one of CATEGORIES
    target_address: str
    target_name: str
    cases: list[AttackCase]
    judge_address: str  # where the ProbeResult goes
    timeout_s: int = 15  # per case


class Exchange(Model):
    """What happened for one case, from the probe's side of the wire."""

    case: AttackCase
    sent: list[str] = []  # chat texts and raw JSON we sent
    received: list[str] = []  # chat texts the target sent back, in order
    events: list[str] = []  # protocol events, see the table above
    sent_chat: bool = True  # we sent at least one ChatMessage (so an ack was expected)
    acked: bool = True  # the target acknowledged our ChatMessage
    latency_ms: int = 0  # until the last reply for this case
    timed_out: bool = False  # the target went silent: the case can't be judged
    error: str = ""  # probe-side failure, e.g. "send failed"


class ProbeResult(Model):
    audit_id: str
    kind: str
    target_address: str
    exchanges: list[Exchange]
    error: str = ""  # whole-probe failure


class JudgeRequest(Model):
    audit_id: str
    target_name: str
    target_address: str
    canary: str = ""  # known for allowlisted targets, empty otherwise
    expected_kinds: list[str]  # probes we wait for
    requester: str  # orchestrator address, receives Progress and Verdict
    deadline_s: int = 80  # then grade what arrived and mark the verdict partial


class ProbeProgress(Model):
    audit_id: str
    kind: str
    passed: int
    failed: int
    skipped: int  # timed out or soft case without asi1
    headline: str  # one line for the progress message


class Finding(Model):
    case_id: str
    title: str
    category: str
    severity: str
    hard: bool
    evidence: str  # exact quote from the transcript
    fix: str
    source: str = "rule"  # "rule" or "asi1"


class ProbeSummary(Model):
    kind: str
    passed: int
    failed: int
    skipped: int


class Verdict(Model):
    audit_id: str
    target_name: str
    target_address: str
    grade: str  # one of GRADES
    score: int  # 0..100
    partial: bool = False  # missing probe, timeout, or asi1 unavailable
    partial_reason: str = ""
    findings: list[Finding] = []  # failed cases only, hard failures first
    sections: list[ProbeSummary] = []
    passed: int = 0
    total: int = 0
    generated_at: float = 0.0
    trace: str = ""  # JSON: per-case wire telemetry for the "View telemetry" report; never used for grading


# ---------------------------------------------------------------------------
# Plan (orchestrator-internal but shared with cards)
# ---------------------------------------------------------------------------


class AuditPlan(Model):
    audit_id: str
    target_name: str
    target_address: str
    cases: list[AttackCase]  # capped at core.config.MAX_CASES_PER_AUDIT
    price_usd: str = "1.00"
    rationale: str = ""  # one line from the planner


class RegistryEntry(Model):
    """What a free trust lookup returns."""

    target_name: str
    target_address: str
    grade: str
    score: int
    partial: bool
    top_findings: list[Finding]
    audited_at: float
    verified: bool = False  # owner proved control, joins the re-verify timer
    last_verified: float = 0.0

