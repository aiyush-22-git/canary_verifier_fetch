"""Chat Protocol plumbing. No agent builds a ChatMessage by hand; they call these.

- text() / card() / ack() build messages
- send_text() / send_card() / send_ack() send them with the [outbound] log line
- make_chat_protocol() builds a Protocol that ACKs first, then routes text to a handler
- parse_selection() understands a card selection arriving as JSON (direct @mention) or as
  prose (ASI:One's planner)
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    MetadataContent,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)

CARD_KINDS = ("carousel", "detail", "form", "review", "custom")
CARD_MAX_BYTES = 64 * 1024
CARD_MAX_DEPTH = 8

TextHandler = Callable[[Context, str, str, ChatMessage], Awaitable[None]]
AckHandler = Callable[[Context, str, ChatAcknowledgement], Awaitable[None]]


# --- builders -----------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)  # datetime.UTC is 3.11+; hosted agents run 3.10


def text(body: str, *, end_session: bool = False) -> ChatMessage:
    content: list[Any] = [TextContent(type="text", text=body)]
    if end_session:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(timestamp=_now(), msg_id=uuid4(), content=content)


def ack(msg: ChatMessage) -> ChatAcknowledgement:
    return ChatAcknowledgement(timestamp=_now(), acknowledged_msg_id=msg.msg_id)


def _depth(node: Any) -> int:
    """Nesting depth of a custom element tree (root counts as 1)."""
    if isinstance(node, dict):
        kids = node.get("children") or []
        sub = [_depth(k) for k in kids]
        if isinstance(node.get("items"), list):
            sub += [_depth(i) for i in node["items"]]
        if "root" in node:
            sub.append(_depth(node["root"]) - 1)
        return 1 + (max(sub) if sub else 0)
    return 0


def card_is_valid(kind: str, payload: dict[str, Any]) -> bool:
    if kind not in CARD_KINDS:
        return False
    raw = json.dumps(payload, separators=(",", ":"))
    if len(raw.encode()) > CARD_MAX_BYTES:
        return False
    if kind == "custom":
        return "root" in payload and _depth(payload["root"]) <= CARD_MAX_DEPTH
    return True


def card(kind: str, payload: dict[str, Any], summary: str, *, terminal: bool = False, width_px: int | None = None) -> ChatMessage:
    """Text summary + one MetadataContent holding the card. If the payload is invalid the message
    degrades to the summary alone, which is also what ASI:One does with a rejected card."""
    if not card_is_valid(kind, payload):
        return text(summary)
    meta = {
        "card_protocol_version": "1",
        "requires_card_interaction": "true",
        "card_kind": kind,
        "card_payload": json.dumps(payload, separators=(",", ":")),
    }
    if terminal:
        meta["is_terminal"] = "true"
    if width_px:
        meta["preferred_drawer_width_px"] = str(max(320, min(800, width_px)))
    return ChatMessage(
        timestamp=_now(),
        msg_id=uuid4(),
        content=[TextContent(type="text", text=summary), MetadataContent(type="metadata", metadata=meta)],
    )


# --- reading messages -----------------------------------------------------------


def extract_text(msg: ChatMessage) -> str:
    return "\n".join(c.text for c in msg.content if isinstance(c, TextContent) and c.text).strip()


def extract_cards(msg: ChatMessage) -> list[dict[str, Any]]:
    """Cards in a message as {"kind": ..., "payload": {...}} (used by probes to log what a target showed)."""
    out: list[dict[str, Any]] = []
    for c in msg.content:
        if not isinstance(c, MetadataContent):
            continue
        kind = c.metadata.get("card_kind")
        if not kind:
            continue
        try:
            payload = json.loads(c.metadata.get("card_payload", "{}"))
        except json.JSONDecodeError:
            payload = {}
        out.append({"kind": kind, "payload": payload})
    return out


_ACTION_WORDS: dict[str, tuple[str, ...]] = {
    "pay_and_run": ("pay_and_run", "pay and run"),
    "view_evidence": ("view_evidence", "view evidence"),
    "retest": ("retest", "re-test"),
    "run_fresh": ("run_fresh", "run a fresh audit", "fresh audit"),
    "open_finding": ("open_finding",),
    "verify": ("verify",),
    "confirm": ("confirm", "approve", "approved"),
    "cancel": ("cancel", "reject", "declined", "decline"),
}
_ADDRESS = re.compile(r"\bagent1[0-9a-z]{20,}\b")
_AUDIT_ID = re.compile(r"\b(?:audit[_ ]?id)?[:=\s\"']*([0-9a-f]{12})\b")
_CASE_ID = re.compile(r"\b(?:case[_ ]?id)[:=\s\"']*([a-z0-9][a-z0-9_-]{2,60})")


def parse_selection(raw: str) -> dict[str, Any]:
    """Normalise a card selection. Always returns a dict with `via` ('json' | 'prose' | 'none')
    and, when recognised, `action`, `audit_id`, `case_id`, `address`.

    JSON arrives on a direct @mention, prose when ASI:One's planner routes the user. Callers must
    still check the result against server-side state; a selection alone proves nothing."""
    stripped = raw.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                data["via"] = "json"
                return data
        except json.JSONDecodeError:
            pass
    low = stripped.lower()
    out: dict[str, Any] = {"via": "none"}
    for action, words in _ACTION_WORDS.items():
        if any(w in low for w in words):
            out["action"] = action
            out["via"] = "prose"
            break
    if m := _AUDIT_ID.search(low):
        out["audit_id"] = m.group(1)
        out["via"] = "prose"
    if m := _CASE_ID.search(low):
        out["case_id"] = m.group(1)
    if m := _ADDRESS.search(stripped):
        out["address"] = m.group(0)
    return out


# --- sending ---------------------------------------------------------------------


def _log_out(ctx: Context, kind: str, to: str, detail: str = "") -> None:
    ctx.logger.info(f"[outbound] {kind} -> {to}{' ' + detail if detail else ''}")


async def send_text(ctx: Context, to: str, body: str, *, end_session: bool = False) -> ChatMessage:
    msg = text(body, end_session=end_session)
    _log_out(ctx, "ChatMessage", to, f"msg_id={msg.msg_id}")
    await ctx.send(to, msg)
    return msg


async def send_card(
    ctx: Context, to: str, kind: str, payload: dict[str, Any], summary: str, *, terminal: bool = False
) -> ChatMessage:
    msg = card(kind, payload, summary, terminal=terminal)
    _log_out(ctx, "ChatMessage", to, f"card={kind} msg_id={msg.msg_id}")
    await ctx.send(to, msg)
    return msg


async def send_ack(ctx: Context, to: str, msg: ChatMessage) -> None:
    _log_out(ctx, "ChatAcknowledgement", to, f"acknowledged_msg_id={msg.msg_id}")
    await ctx.send(to, ack(msg))


# --- protocol factory ---------------------------------------------------------------


def make_chat_protocol(on_text: TextHandler, on_ack: AckHandler | None = None) -> Protocol:
    """Chat Protocol with the Fetch conventions: ACK first, iterate content, never crash on
    unknown content. `on_text(ctx, sender, text, msg)` is the only thing an agent supplies."""
    proto = Protocol(spec=chat_protocol_spec)

    @proto.on_message(ChatMessage)
    async def _on_chat(ctx: Context, sender: str, msg: ChatMessage) -> None:
        ctx.logger.info(f"[inbound] ChatMessage from {sender} msg_id={msg.msg_id}")
        await send_ack(ctx, sender, msg)
        body = extract_text(msg)
        for item in msg.content:
            if isinstance(item, StartSessionContent):
                ctx.logger.info(f"[state] session_started sender={sender}")
            elif isinstance(item, EndSessionContent):
                ctx.logger.info(f"[state] session_ended sender={sender}")
            elif not isinstance(item, (TextContent, MetadataContent)):
                ctx.logger.warning(f"[inbound] unknown content from {sender}: {type(item).__name__}")
        if body:
            ctx.logger.info(f"[inbound] text from {sender}: {body[:120]!r}")
            await on_text(ctx, sender, body, msg)

    @proto.on_message(ChatAcknowledgement)
    async def _on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
        ctx.logger.info(f"[inbound] ChatAcknowledgement from {sender} acknowledged_msg_id={msg.acknowledged_msg_id}")
        if on_ack:
            await on_ack(ctx, sender, msg)

    return proto
