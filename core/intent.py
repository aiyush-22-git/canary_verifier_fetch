"""Chat message -> action. asi1 tool calling decides; a keyword fallback keeps the flow alive
when asi1 is slow or down (the plan's "rules, then model" order, applied to intent)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from core import llm
from core.registry import Registry

ADDRESS = re.compile(r"\bagent1[0-9a-z]{20,}\b")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_trust",
            "description": "Look up the existing trust score of an Agentverse agent. Free. Use for 'is X safe', "
            "'can I trust X', 'what is X's grade'.",
            "parameters": {
                "type": "object",
                "properties": {"agent": {"type": "string", "description": "Agent name or agent1q... address"}},
                "required": ["agent"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_audit",
            "description": "Run a fresh paid security audit of an agent. Use for 'audit X', 'test X', 'run a new audit', "
            "'re-test X'.",
            "parameters": {
                "type": "object",
                "properties": {"agent": {"type": "string", "description": "Agent name or agent1q... address"}},
                "required": ["agent"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_owner",
            "description": "The user says they own an agent and wants it verified so it can be audited and re-tested.",
            "parameters": {
                "type": "object",
                "properties": {"agent": {"type": "string", "description": "Agent address if given, else empty"}},
            },
        },
    },
]

SYSTEM = (
    "You are Canary, a trust auditor for Agentverse agents. Pick exactly one tool when the user wants to look up, audit "
    "or verify a specific agent. For anything else (a greeting, or a question about how Canary works) call no tool and "
    "answer in at most four short sentences. Facts you may use: Canary attacks an agent live with prompt-injection, data-leak "
    "and payment-fraud tests and grades it A to F with exact quotes as evidence. Any hard failure (a leaked secret, an order "
    "delivered without a verified payment, a missing acknowledgement) is an automatic F. A partial audit means some probes "
    "or the AI grader could not finish, so the grade is capped at B and labelled partial. Never invent other features."
)


@dataclass
class Intent:
    action: str  # check_trust | start_audit | verify_owner | help
    agent: str = ""
    source: str = "asi1"  # or "keywords"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def resolve_agent(reg: Registry, ref: str, message: str = "") -> str | None:
    """Turn a name or address into a registry address. Looks at the model's `agent` argument first,
    then scans the raw message for an address or a known agent name. None if unknown."""
    for candidate in (ref, message):
        if not candidate:
            continue
        if m := ADDRESS.search(candidate):
            return m.group(0)
    for candidate in (ref, message):
        if not candidate:
            continue
        row = reg.find_agent(candidate.strip())
        if row:
            return row["address"]
    haystack = _norm(f"{ref} {message}")
    best: tuple[int, str] | None = None
    for row in reg.all_agents():
        name = _norm(row["name"])
        if name and name in haystack and (best is None or len(name) > best[0]):
            best = (len(name), row["address"])
    return best[1] if best else None


def keyword_intent(message: str) -> Intent:
    low = message.lower()
    if re.search(r"\b(verify|my agent|i own|i built|claim)\b", low):
        return Intent("verify_owner", message, "keywords")
    if re.search(r"\b(audit|test|re-?test|run|scan|probe)\b", low):
        return Intent("start_audit", message, "keywords")
    if re.search(r"\b(safe|trust|score|grade|rating|check|lookup|is)\b", low) or ADDRESS.search(low):
        return Intent("check_trust", message, "keywords")
    return Intent("help", "", "keywords")


GREETING = re.compile(r"^\s*(hi|hello|hey|yo|hola|help|start|menu|what can you do)\b[\s\w?!.,]{0,20}$", re.I)


async def classify(message: str) -> Intent:
    if GREETING.match(message):  # a greeting never needs a model: answer in milliseconds, not after an asi1 round trip
        return Intent("help", "", "keywords")
    if llm.available():
        picked = await llm.tool_call(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": message}], TOOLS
        )
        if picked:
            name, args = picked
            if name == llm.PROSE:  # a question about Canary itself: the answer is the reply, not an audit
                return Intent("answer", str(args.get("text") or ""), "asi1")
            return Intent(name, str(args.get("agent") or ""), "asi1")
    return keyword_intent(message)
