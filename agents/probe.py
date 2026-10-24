"""Probe agents (B2): one class, three instances (`injection`, `leak`, `payment`).

A probe receives a ProbeRequest, attacks the target case by case with what the pack says (it sends no attack text of
its own), records what happened as an Exchange and sends one ProbeResult to the Judge. It never judges anything and
never moves funds: every CommitPayment it sends carries a made-up transaction id.

Event-driven and stateless by construction (it must also run as an Agentverse hosted agent, where globals reset after
every invocation and handlers must not wait): the whole run lives in `ctx.storage` as plain JSON. The ProbeRequest handler
stores the run, sends the first message and returns. Every message from the target (chat reply, ACK, payment message)
is recorded and moves the run on; the interval tick fails over silent cases (`timed_out`) and sends the ProbeResult when
the last case is done. Silence is data, not an exception.

    handle_request -> machine.start/begin ---> send first message
    handle_item    -> machine.on_item -> (short settle) -> machine.advance ---> next message / commit / next case
    tick           -> machine.expire  ---> silent step = timed_out, deadlines, missed wake-ups
    finished run   -> ProbeResult to the Judge, state cleared, queued request for the same target starts
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any

from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage
from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    Funds,
    RequestPayment,
    payment_protocol_spec,
)

from core import chat, config
from core.agentkit import make_agent
from core.models import (
    EVENT_ACK,
    EVENT_CANCEL_PAYMENT,
    EVENT_COMMIT_PAYMENT,
    EVENT_COMPLETE_PAYMENT,
    EVENT_REJECT_PAYMENT,
    EVENT_REQUEST_PAYMENT,
    AttackCase,
    Exchange,
    ProbeRequest,
    ProbeResult,
)

SETTLE_S = 0.6  # after a reply, how long we keep listening for follow-up messages of the same turn
SETTLE_GRACE_S = 1.5  # the tick finishes a settle whose wake-up was lost (hosted agents reset between invocations)
REQUEST_WAIT_S = 4.0  # after the last purchase message, how long we wait for a RequestPayment
OUTCOME_WAIT_S = 4.0  # after a CommitPayment, how long a non-terminal reply may precede Complete/Cancel/Reject
TICK_S = 2.0
SHORT_AMOUNT = "0.01"
BUYER_WALLET = "fetch1canaryprobebuyer000000000000000000000"  # a label: this wallet never holds or sends funds

CHAT_ACTIONS = ("chat", "raw_json", "tool_injection")
PAY_ACTIONS = ("pay_fake", "pay_replay", "pay_wrong_amount")
SUPPORTED = (*CHAT_ACTIONS, *PAY_ACTIONS, "service_before_pay")
TERMINAL = ("complete", "cancel", "reject")
MAX_ACKS = 500
ACK_GRACE_S = 6.0  # hosted delivery is unordered: an ACK may land after the reply, so a finished run waits this long for it

Out = dict[str, Any]  # {"type": "chat", "msg": ChatMessage} or {"type": "commit", ...}


# --- the state machine: pure functions over one run dict (JSON in, JSON out) -------------------------------------


def start(req: ProbeRequest, now: float) -> dict[str, Any]:
    return {
        "audit_id": req.audit_id, "kind": req.kind, "target_address": req.target_address, "target_name": req.target_name,
        "judge_address": req.judge_address, "timeout_s": req.timeout_s,
        "cases": [c.dict() for c in req.cases], "idx": 0, "exchanges": [], "cur": None, "acked": [],
        "late_acks": {}, "finished": False, "finished_at": 0.0, "started": now,
    }


def _new_cur(case: dict[str, Any], now: float) -> dict[str, Any]:
    steps = list(case["messages"])
    if case["action"] == "raw_json":
        steps.append(case["raw_json"])  # the spoofed card selection, verbatim
    mode = "chat" if case["action"] in CHAT_ACTIONS else "sbp" if case["action"] == "service_before_pay" else "pay"
    return {
        "case": case, "mode": mode, "steps": steps, "i": -1, "phase": "start", "round": 0, "tx": "", "terminal": False,
        "step_replied": False, "timed_out": False, "deadline": 0.0, "settle_at": 0.0, "started": now, "last_at": now,
        "activity": 0, "sent": [], "sent_ids": [], "received": [], "events": [], "requests": [], "req_seen": 0,
    }


def _send_next(run: dict[str, Any], cur: dict[str, Any], now: float) -> list[Out]:
    cur["i"] += 1
    body = cur["steps"][cur["i"]]
    msg = chat.text(body)
    cur["sent"].append(body)
    cur["sent_ids"].append(str(msg.msg_id))
    cur["phase"], cur["step_replied"], cur["settle_at"], cur["deadline"] = "say", False, 0.0, now + run["timeout_s"]
    return [{"type": "chat", "msg": msg}]


def _unseen_request(cur: dict[str, Any]) -> dict[str, Any] | None:
    if cur["req_seen"] < len(cur["requests"]):
        cur["req_seen"] += 1
        return cur["requests"][cur["req_seen"] - 1]
    return None


def _commit(run: dict[str, Any], cur: dict[str, Any], request: dict[str, Any], now: float) -> list[Out]:
    action = cur["case"]["action"]
    kind = "short" if action == "pay_wrong_amount" else "replay" if cur["round"] == 1 else "fake"
    if cur["round"] == 0:
        cur["tx"] = "0x" + secrets.token_hex(32)
    amount = SHORT_AMOUNT if kind == "short" else request["amount"]
    cur["events"].append(f"{EVENT_COMMIT_PAYMENT} kind={kind} tx={cur['tx']} amount={amount}")
    cur["phase"], cur["terminal"], cur["settle_at"], cur["deadline"] = "outcome", False, 0.0, now + run["timeout_s"]
    return [{"type": "commit", "kind": kind, "tx": cur["tx"], "amount": amount, "request": request}]


def begin(run: dict[str, Any], now: float) -> list[Out]:
    """Start the case at run['idx'] (skipping unsupported ones); finishes the run when none is left."""
    while run["idx"] < len(run["cases"]):
        case = run["cases"][run["idx"]]
        if case["action"] in SUPPORTED:
            cur = _new_cur(case, now)
            run["cur"] = cur
            if cur["steps"]:
                return _send_next(run, cur, now)
            if cur["mode"] == "pay":
                cur["phase"], cur["deadline"] = "await_request", now + min(run["timeout_s"], REQUEST_WAIT_S)
                return []
            return _finish_case(run, now)
        run["exchanges"].append(Exchange(case=AttackCase(**case), error="unsupported action").dict())
        run["idx"] += 1
    run["cur"], run["finished"], run["finished_at"] = None, True, now
    return []


def apply_late_acks(run: dict[str, Any]) -> int:
    """Mark closed exchanges acknowledged once all their ACKs have arrived. Returns how many still wait for one."""
    for key, ids in list(run["late_acks"].items()):
        if all(i in run["acked"] for i in ids):
            ex = run["exchanges"][int(key)]
            ex["acked"], ex["events"] = True, [EVENT_ACK, *ex["events"]]
            del run["late_acks"][key]
    return len(run["late_acks"])


def _finish_case(run: dict[str, Any], now: float) -> list[Out]:
    cur = run["cur"]
    sent_chat = bool(cur["sent_ids"])
    acked = all(i in run["acked"] for i in cur["sent_ids"])
    silent = cur["timed_out"] or (sent_chat and cur["activity"] == 0)  # nothing at all came back: the case can't be judged
    run["exchanges"].append(Exchange(
        case=AttackCase(**cur["case"]), sent=cur["sent"], received=cur["received"],
        events=([EVENT_ACK] if sent_chat and acked else []) + cur["events"],
        sent_chat=sent_chat, acked=acked, latency_ms=int((cur["last_at"] - cur["started"]) * 1000) if cur["activity"] else 0,
        timed_out=silent,
    ).dict())
    if sent_chat and not acked:
        run["late_acks"][str(len(run["exchanges"]) - 1)] = cur["sent_ids"]
    run["idx"] += 1
    run["cur"] = None
    return begin(run, now)


def on_item(run: dict[str, Any], kind: str, payload: dict[str, Any], now: float) -> str | None:
    """Record something the target sent. Returns "settle" (advance after a short quiet period), "now", or None."""
    cur = run["cur"]
    if cur is None:
        return None
    cur["activity"] += 1
    cur["last_at"] = now
    if kind == "text":
        cur["received"].append(payload["body"])
        cur["events"].extend(payload.get("events", []))
    elif kind == "request":
        cur["events"].append(f"{EVENT_REQUEST_PAYMENT} amount={payload['amount']} currency={payload['currency']}")
        cur["requests"].append(payload)
    elif kind == "complete":
        cur["events"].append(f"{EVENT_COMPLETE_PAYMENT} tx={payload['tx']}")
        cur["terminal"] = True
    elif kind in ("cancel", "reject"):
        label = EVENT_CANCEL_PAYMENT if kind == "cancel" else EVENT_REJECT_PAYMENT
        cur["events"].append(f"{label} reason={payload.get('reason', '')}")
        cur["terminal"] = True
    phase = cur["phase"]
    if phase == "say":
        cur["step_replied"], cur["settle_at"] = True, now + SETTLE_S
        return "settle"
    if phase == "await_request" and kind == "request":
        return "now"
    if phase == "outcome":
        if cur["terminal"]:
            cur["settle_at"] = now + SETTLE_S
            return "settle"
        if kind == "text":
            cur["deadline"] = now + OUTCOME_WAIT_S
    return None


def token(run: dict[str, Any]) -> list[Any] | None:
    """Identifies the wait a settle belongs to: if anything newer arrived, the newer settle takes over."""
    cur = run["cur"]
    return None if cur is None else [run["idx"], cur["phase"], cur["i"], cur["round"], cur["last_at"]]


def advance(run: dict[str, Any], now: float) -> list[Out]:
    cur = run["cur"]
    if cur is None:
        return []
    cur["settle_at"] = 0.0
    phase, mode = cur["phase"], cur["mode"]
    if phase == "say":
        if mode == "pay" and (request := _unseen_request(cur)) is not None:
            return _commit(run, cur, request, now)
        if cur["i"] + 1 < len(cur["steps"]):
            return _send_next(run, cur, now)
        if mode == "pay":
            cur["phase"], cur["deadline"] = "await_request", now + min(run["timeout_s"], REQUEST_WAIT_S)
            return []
        return _finish_case(run, now)
    if phase == "await_request":
        request = _unseen_request(cur)
        return _commit(run, cur, request, now) if request is not None else _finish_case(run, now)
    if phase == "outcome":
        if cur["case"]["action"] == "pay_replay" and cur["round"] == 0:  # a second order reusing the same transaction id
            cur.update(round=1, i=-1, terminal=False)
            return _send_next(run, cur, now)
        return _finish_case(run, now)
    return []


def expire(run: dict[str, Any], now: float) -> list[Out]:
    """Called by the tick: lost wake-ups, silent steps, and wait deadlines."""
    cur = run["cur"]
    if cur is None:
        return []
    if cur["settle_at"]:
        return advance(run, now) if now >= cur["settle_at"] + SETTLE_GRACE_S else []
    if cur["phase"] == "say" and not cur["step_replied"] and now >= cur["deadline"]:
        if cur["mode"] == "chat":
            cur["timed_out"] = True
            return _finish_case(run, now)
        if (request := _unseen_request(cur)) is not None:
            return _commit(run, cur, request, now)
        return _finish_case(run, now)
    if cur["phase"] in ("await_request", "outcome") and now >= cur["deadline"]:
        return advance(run, now)
    return []


def result_of(run: dict[str, Any]) -> ProbeResult:
    return ProbeResult(
        audit_id=run["audit_id"], kind=run["kind"], target_address=run["target_address"],
        exchanges=[Exchange.parse_obj(e) for e in run["exchanges"]],
    )


# --- storage and sending ---------------------------------------------------------------------------------------------


def _get(ctx: Context, key: str, default: Any) -> Any:
    value = ctx.storage.get(key)
    return default if value is None else value


def _load(ctx: Context, address: str) -> dict[str, Any] | None:
    return ctx.storage.get(f"run:{address}")


def _targets(ctx: Context) -> list[str]:
    return list(_get(ctx, "targets", []))


def _save(ctx: Context, address: str, run: dict[str, Any]) -> None:
    run["_stored"] = True
    ctx.storage.set(f"run:{address}", run)
    targets = _targets(ctx)
    if address not in targets:
        ctx.storage.set("targets", [*targets, address])


def _clear(ctx: Context, address: str) -> None:
    ctx.storage.remove(f"run:{address}")
    ctx.storage.set("targets", [t for t in _targets(ctx) if t != address])


async def _send(ctx: Context, address: str, outs: list[Out]) -> None:
    for out in outs:
        if out["type"] == "chat":
            ctx.logger.info(f"[outbound] ChatMessage -> {address} msg_id={out['msg'].msg_id}")
            await ctx.send(address, out["msg"])
            continue
        request = out["request"]
        commit = CommitPayment(
            funds=Funds(currency=request["currency"], amount=out["amount"], payment_method=request["method"]),
            recipient=request["recipient"], transaction_id=out["tx"], reference=request["reference"],
            description=f"Canary probe ({out['kind']}): no funds moved", metadata={"buyer_fet_wallet": BUYER_WALLET},
        )
        ctx.logger.info(f"[outbound] CommitPayment -> {address} kind={out['kind']} tx={out['tx']} amount={out['amount']}")
        await ctx.send(address, commit)


async def _flush(ctx: Context, address: str, run: dict[str, Any], outs: list[Out]) -> None:
    """Persist first, then send: a handler that dies after the save still leaves a consistent run."""
    if not run["finished"]:
        _save(ctx, address, run)
        await _send(ctx, address, outs)
        return
    if run.get("_stored") and _load(ctx, address) is None:  # the tick and a message handler can both reach this point: only the first reports
        return
    if apply_late_acks(run) and time.time() < run["finished_at"] + ACK_GRACE_S:
        _save(ctx, address, run)  # the tick flushes again until the ACKs arrive or the grace period ends
        return
    _clear(ctx, address)
    ctx.logger.info(f"[outbound] ProbeResult -> {run['judge_address']} audit={run['audit_id']} exchanges={len(run['exchanges'])}")
    await ctx.send(run["judge_address"], result_of(run))
    queued = _get(ctx, f"queue:{address}", [])
    if queued:  # another audit of the same target was waiting: one conversation per target at a time
        ctx.storage.set(f"queue:{address}", queued[1:])
        await _launch(ctx, ProbeRequest.parse_obj(queued[0]))


async def _launch(ctx: Context, req: ProbeRequest) -> None:
    run = start(req, time.time())
    outs = begin(run, time.time())
    await _flush(ctx, req.target_address, run, outs)


# --- handlers ----------------------------------------------------------------------------------------------------------


async def handle_request(ctx: Context, sender: str, req: ProbeRequest) -> None:
    ctx.logger.info(f"[inbound] ProbeRequest audit={req.audit_id} kind={req.kind} cases={len(req.cases)}")
    if _load(ctx, req.target_address) is not None:
        ctx.storage.set(f"queue:{req.target_address}", [*_get(ctx, f"queue:{req.target_address}", []), json.loads(req.json())])
        return
    await _launch(ctx, req)


async def _advance(ctx: Context, address: str, wanted: list[Any] | None) -> None:
    run = _load(ctx, address)
    if run is None or (wanted is not None and token(run) != wanted):
        return  # a newer message arrived: its own settle will advance
    outs = advance(run, time.time())
    await _flush(ctx, address, run, outs)


async def handle_item(ctx: Context, address: str, kind: str, payload: dict[str, Any]) -> None:
    run = _load(ctx, address)
    if run is None:
        return
    decision = on_item(run, kind, payload, time.time())
    wanted = token(run)
    _save(ctx, address, run)
    if decision == "now":
        await _advance(ctx, address, None)
    elif decision == "settle":
        await asyncio.sleep(SETTLE_S)  # short quiet period for the rest of the target's turn, then move on
        await _advance(ctx, address, wanted)


async def tick(ctx: Context) -> None:
    for address in _targets(ctx):
        run = _load(ctx, address)
        if run is None:
            _clear(ctx, address)
            continue
        outs = expire(run, time.time())
        await _flush(ctx, address, run, outs)


async def on_text(ctx: Context, sender: str, body: str, msg: ChatMessage) -> None:
    events = []
    for card in chat.extract_cards(msg):
        actions: list[str] = []
        _card_actions(card["payload"], actions)
        events.append(f"Card kind={card['kind']} actions={','.join(actions)}")
    await handle_item(ctx, sender, "text", {"body": body, "events": events})


async def on_ack(ctx: Context, sender: str, ack: ChatAcknowledgement) -> None:
    run = _load(ctx, sender)
    if run is not None:
        run["acked"] = [*run["acked"], str(ack.acknowledged_msg_id)][-MAX_ACKS:]
        if run["finished"]:
            await _flush(ctx, sender, run, [])  # the last missing ACK releases the result early
        else:
            _save(ctx, sender, run)


async def on_request_payment(ctx: Context, sender: str, msg: RequestPayment) -> None:
    ctx.logger.info(f"[inbound] RequestPayment from {sender} ref={msg.reference}")
    if not msg.accepted_funds:  # nothing to answer: record nothing rather than inventing an amount
        return
    funds = msg.accepted_funds[0]
    await handle_item(ctx, sender, "request", {
        "amount": funds.amount, "currency": funds.currency, "method": funds.payment_method,
        "recipient": msg.recipient, "reference": msg.reference,
    })


async def on_complete(ctx: Context, sender: str, msg: CompletePayment) -> None:
    ctx.logger.info(f"[inbound] CompletePayment from {sender} tx={msg.transaction_id}")
    await handle_item(ctx, sender, "complete", {"tx": msg.transaction_id})


async def on_cancel(ctx: Context, sender: str, msg: CancelPayment) -> None:
    ctx.logger.info(f"[inbound] CancelPayment from {sender}")
    await handle_item(ctx, sender, "cancel", {"reason": msg.reason or ""})


def _card_actions(node: object, out: list[str]) -> None:
    """Every `action` named anywhere in a card payload (including selections stored as JSON strings)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "action" and isinstance(value, str) and value not in out:
                out.append(value)
            else:
                _card_actions(value, out)
    elif isinstance(node, list):
        for item in node:
            _card_actions(item, out)
    elif isinstance(node, str) and node.startswith("{"):
        try:
            _card_actions(json.loads(node), out)
        except json.JSONDecodeError:
            pass


def attach(agent, kind: str) -> None:
    """Register the probe's handlers on any agent object (the local Bureau's, or an Agentverse hosted agent)."""
    agent.include(chat.make_chat_protocol(on_text, on_ack), publish_manifest=False)
    buyer = Protocol(spec=payment_protocol_spec, role="buyer")
    buyer.on_message(RequestPayment)(on_request_payment)
    buyer.on_message(CompletePayment)(on_complete)
    buyer.on_message(CancelPayment)(on_cancel)  # the buyer role of the spec receives Request, Complete and Cancel only
    agent.include(buyer, publish_manifest=False)
    agent.on_message(ProbeRequest)(handle_request)
    agent.on_interval(period=TICK_S)(tick)


def build_probe(kind: str):
    """The local agent for one attack family. `kind` is one of core.models.CATEGORIES."""
    agent = make_agent(
        config.probe_key(kind),
        description=f"Canary {kind} probe: runs the {kind} attack pack against an opted-in agent when asked by Canary-Orchestrator.",
        readme_path=f"docs/agentverse/probe-{kind}.md",
    )
    attach(agent, kind)
    return agent
