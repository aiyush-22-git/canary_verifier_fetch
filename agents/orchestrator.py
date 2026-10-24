"""Orchestrator: the only agent ASI:One talks to.

Chat Protocol (intent, cards) + Payment Protocol as seller + the re-verify timer. Every card
selection is checked against server-side state in the registry; a selection alone never starts
an audit or releases a result. Handlers only route: logic lives in core/.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from uagents import Context, Protocol
from uagents.setup import fund_agent_if_low
from uagents_core.contrib.protocols.chat import ChatMessage
from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    RejectPayment,
    payment_protocol_spec,
)

from core import cards, chat, config, intent, payments, planner
from core.agentkit import make_agent
from core.models import AuditPlan, JudgeRequest, ProbeProgress, ProbeRequest, Verdict
from core.packs import load_cases
from core.registry import Registry, get_registry

HELP = (
    "I'm Canary, a trust auditor for Agentverse agents. Before you pay an agent, ask me:\n"
    "- \"Is ShopBot-Solid safe to pay?\": instant, free if I've already tested it\n"
    "- \"Audit ShopBot-Overcharge\": a live paid audit with real attacks, graded A to F with quotes\n"
    "- \"I own agent1q...\": verify your agent so it's re-tested on a timer\n"
    "Demo agents available: ShopBot-Leaky, ShopBot-Overcharge, ShopBot-Solid."
)


@dataclass
class ActiveAudit:
    plan: AuditPlan
    requester: str
    paid_tx: str = ""
    system: bool = False  # re-verify run: no user to message
    started: float = 0.0


def build_orchestrator(registry: Registry | None = None):
    reg = registry or get_registry()
    agent = make_agent(
        "orchestrator",
        mailbox=True,
        description="Canary: paid trust audits for Agentverse agents. Ask 'is <agent> safe?' before you pay one.",
        readme_path="docs/agentverse/orchestrator.md",
    )
    if config.mailbox_enabled() and config.NETWORK == "testnet":
        fund_agent_if_low(str(agent.wallet.address()))

    active: dict[str, ActiveAudit] = {}
    wallet = str(agent.wallet.address())
    judge_addr = config.agent_address("judge")

    # --- helpers --------------------------------------------------------------------------

    async def notice(ctx: Context, to: str, title: str, what: str, nxt: str, **kw) -> None:
        await chat.send_card(ctx, to, *cards.notice(title, what, nxt, **kw))

    async def offer_plan(ctx: Context, to: str, address: str) -> None:
        row = reg.find_agent(address)
        if not row or not row["allowed"]:
            await notice(
                ctx, to, "This agent hasn't opted in",
                "I only test agents whose owners have opted in, so nobody gets attacked without consent.",
                "If you own it, tell me 'I own <address>' and I'll give you a one-time code to prove it.",
                action="claim_agent_help", cta_label="Verify ownership",
            )
            return
        plan = await planner.plan_audit(row["name"], row["address"], load_cases())
        reg.create_pending(plan.audit_id, to, plan.json())
        await chat.send_card(ctx, to, *cards.audit_plan(plan, rail_label=payments.rail_label()))

    async def lookup(ctx: Context, to: str, address: str) -> None:
        entry = reg.latest(address)
        if entry is None:
            row = reg.find_agent(address)
            if row and row["allowed"]:
                await chat.send_text(ctx, to, f"I haven't tested {row['name']} yet. Here is the plan for a first audit.")
            await offer_plan(ctx, to, address)
            return
        await chat.send_card(ctx, to, *cards.trust_lookup(entry))

    async def start_run(ctx: Context, requester: str, plan: AuditPlan, paid_tx: str, *, system: bool = False) -> None:
        """Fan out: JudgeRequest first (so it knows what to expect), then one ProbeRequest per family."""
        by_kind = planner.split_by_kind(plan)
        canary = reg.canary_for(plan.target_address)
        active[plan.audit_id] = ActiveAudit(plan, requester, paid_tx, system, time.time())
        if not system:
            await chat.send_text(ctx, requester, f"Payment confirmed. Testing {plan.target_name} live: "
                                 f"{len(plan.cases)} tests across {len(by_kind)} probes. Results appear as each probe finishes.")
        judge_req = JudgeRequest(
            audit_id=plan.audit_id, target_name=plan.target_name, target_address=plan.target_address,
            canary=canary, expected_kinds=list(by_kind), requester=str(agent.address), deadline_s=config.AUDIT_TIMEOUT_S,
        )
        ctx.logger.info(f"[outbound] JudgeRequest -> {judge_addr} audit={plan.audit_id}")
        await ctx.send(judge_addr, judge_req)
        for kind, kind_cases in by_kind.items():
            req = ProbeRequest(
                audit_id=plan.audit_id, kind=kind, target_address=plan.target_address, target_name=plan.target_name,
                cases=kind_cases, judge_address=judge_addr, timeout_s=config.PROBE_TIMEOUT_S,
            )
            addr = config.agent_address(config.probe_key(kind))
            ctx.logger.info(f"[outbound] ProbeRequest -> {addr} kind={kind} cases={len(kind_cases)}")
            await ctx.send(addr, req)
        asyncio.create_task(watchdog(ctx, plan.audit_id))

    async def watchdog(ctx: Context, audit_id: str) -> None:
        """If the Judge never answers, tell the user instead of leaving them hanging."""
        await asyncio.sleep(config.AUDIT_TIMEOUT_S + 20)
        job = active.pop(audit_id, None)
        if job is None:
            return
        reg.claim_pending(audit_id, job.requester, "running", "failed")
        ctx.logger.error(f"[error] audit {audit_id} got no verdict")
        if not job.system:
            await notice(
                ctx, job.requester, "The audit didn't finish",
                f"{job.plan.target_name} or one of my probes stopped responding before a verdict was ready.",
                "You've already paid for this audit, so you can re-run it without paying again.",
                action="retry_audit", cta_label="Re-run my audit", audit_id=audit_id,
            )

    # --- selections ------------------------------------------------------------------------------

    async def handle_selection(ctx: Context, sender: str, sel: dict) -> bool:
        """True if the message was a recognised card selection (handled, even if refused)."""
        action, audit_id, address = sel.get("action"), str(sel.get("audit_id") or ""), str(sel.get("address") or "")

        if action == "pay_and_run":
            row = reg.get_pending(audit_id, sender)
            if not row or row["state"] != "awaiting_payment":
                await notice(ctx, sender, "That audit plan has expired", "I can't find an open plan for it.",
                             "Ask me to audit the agent again and I'll make a fresh plan.")
                return True
            plan = AuditPlan.parse_raw(row["plan_json"])
            if not reg.is_allowed(plan.target_address):
                await notice(ctx, sender, "Agent not allowlisted", "The owner has not opted in.", "Ask the owner to verify it.")
                return True
            req = payments.build_request(plan, sender, wallet)
            if req is None:
                await notice(
                    ctx, sender, "Payments aren't available right now",
                    f"The {payments.rail_label()} rail isn't configured on my side, so I can't take payment.",
                    "Try again in a few minutes. I never run a paid audit without a verified payment.",
                    action="pay_and_run", cta_label="Try again", audit_id=audit_id,
                )
                return True
            ctx.logger.info(f"[outbound] RequestPayment -> {sender} audit={audit_id}")
            await ctx.send(sender, req)
            return True

        if action == "cancel":
            row = reg.get_pending(audit_id, sender)
            if row and reg.claim_pending(audit_id, sender, "awaiting_payment", "cancelled"):
                await chat.send_text(ctx, sender, "Cancelled. Nothing was charged.")
            else:
                await chat.send_text(ctx, sender, "There's nothing open to cancel.")
            return True

        if action == "retry_audit":
            row = reg.get_pending(audit_id, sender)
            if row and reg.claim_pending(audit_id, sender, "failed", "running"):
                await start_run(ctx, sender, AuditPlan.parse_raw(row["plan_json"]), row["payment_ref"])
            else:
                await notice(ctx, sender, "Can't re-run that audit", "It isn't marked as failed, or it expired.",
                             "Ask me for a new audit.")
            return True

        if action in ("run_fresh", "retest") and address:
            await offer_plan(ctx, sender, address)
            return True

        if action == "view_evidence":
            v = reg.get_verdict(audit_id)
            if v is None or not v.findings:
                await chat.send_text(ctx, sender, "That audit has no findings to show.")
            else:
                await chat.send_card(ctx, sender, *cards.evidence_overview(v))
            return True

        if action == "open_finding":
            v = reg.get_verdict(audit_id)
            f = next((x for x in (v.findings if v else []) if x.case_id == sel.get("case_id")), None)
            if v and f:
                await chat.send_card(ctx, sender, *cards.finding_detail(f, v.audit_id, v.target_address))
            else:
                await chat.send_text(ctx, sender, "I couldn't find that finding.")
            return True

        if action in ("claim_agent_help",):
            await chat.send_card(ctx, sender, *cards.owner_claim_form())
            return True

        if action == "claim_agent" and address:
            if not intent.ADDRESS.fullmatch(address):
                await notice(ctx, sender, "That doesn't look like an agent address",
                             "Agent addresses start with agent1q.", "Paste the address from the agent's Agentverse page.",
                             action="claim_agent_help", cta_label="Try again")
                return True
            code = reg.issue_owner_code(address, sender)
            await chat.send_card(ctx, sender, *cards.owner_code_card(address, code))
            return True

        if action == "verify" and address:
            profile = await planner.fetch_profile("", address)
            if reg.confirm_owner_code(address, sender, profile):
                await chat.send_text(ctx, sender, "Verified. This agent is now on the allowlist and will be re-tested "
                                     "automatically. Say 'audit it' whenever you want a fresh paid audit.")
            else:
                await notice(
                    ctx, sender, "I couldn't find the code yet",
                    "The code wasn't in the agent's public Agentverse profile (or it expired, or it isn't yours).",
                    "Paste the code into the agent's description, wait a minute, then press Verify again.",
                    action="verify", cta_label="Verify again", address=address,
                )
            return True

        if action in ("cancel_verify", "dismiss"):
            await chat.send_text(ctx, sender, "OK.")
            return True
        return False

    # --- chat ------------------------------------------------------------------------------------------

    async def on_text(ctx: Context, sender: str, body: str, msg: ChatMessage) -> None:
        sel = chat.parse_selection(body)
        if sel.get("action") and (sel["via"] == "json" or sel.get("audit_id") or sel.get("address")):
            if await handle_selection(ctx, sender, sel):
                return
        it = await intent.classify(body)
        ctx.logger.info(f"[state] intent={it.action} via={it.source}")
        if it.action == "verify_owner":
            addr = intent.resolve_agent(reg, it.agent, body)
            if addr and intent.ADDRESS.fullmatch(addr):
                await handle_selection(ctx, sender, {"action": "claim_agent", "address": addr})
            else:
                await chat.send_card(ctx, sender, *cards.owner_claim_form())
            return
        if it.action in ("check_trust", "start_audit"):
            addr = intent.resolve_agent(reg, it.agent, body)
            if addr is None:
                await notice(ctx, sender, "I don't know that agent",
                             "I only audit agents that have opted in, and I couldn't match your message to one.",
                             "Give me its agent1q... address, or the exact name. Try ShopBot-Solid.")
                return
            if it.action == "start_audit":
                await offer_plan(ctx, sender, addr)
            else:
                await lookup(ctx, sender, addr)
            return
        await chat.send_text(ctx, sender, HELP)

    agent.include(chat.make_chat_protocol(on_text), publish_manifest=True)

    # --- payment protocol (seller) -----------------------------------------------------------------------

    payment_proto = Protocol(spec=payment_protocol_spec, role="seller")

    async def cancel(ctx: Context, to: str, tx_id: str, reason: str) -> None:
        ctx.logger.info(f"[outbound] CancelPayment -> {to} reason={reason}")
        await ctx.send(to, CancelPayment(transaction_id=tx_id or None, reason=reason))

    @payment_proto.on_message(CommitPayment)
    async def on_commit(ctx: Context, sender: str, msg: CommitPayment) -> None:
        tx = (msg.transaction_id or "").strip()
        ctx.logger.info(f"[inbound] CommitPayment from {sender} tx={tx or 'MISSING'} ref={msg.reference} "
                        f"amount={msg.funds.amount} {msg.funds.currency}")
        row = reg.get_pending(msg.reference, sender) if msg.reference else reg.latest_pending(sender)
        if not row or row["state"] != "awaiting_payment":
            await cancel(ctx, sender, tx, "No audit is waiting for payment from you.")
            return
        audit_id = row["audit_id"]
        if reg.payment_seen(tx):
            ctx.logger.warning(f"[state] replayed transaction {tx} rejected")
            await cancel(ctx, sender, tx, "This transaction id was already used.")
            return
        result = await asyncio.to_thread(payments.verify_commit, msg, sender, audit_id, wallet)
        if not result.ok:
            ctx.logger.warning(f"[state] payment not verified tx={tx}: {result.reason}")
            await cancel(ctx, sender, tx, f"Payment not verified: {result.reason}")
            return
        if not reg.record_payment(tx, sender, audit_id, result.method):
            await cancel(ctx, sender, tx, "This transaction id was already used.")
            return
        if not reg.claim_pending(audit_id, sender, "awaiting_payment", "running", payment_ref=tx):
            await cancel(ctx, sender, tx, "This audit was already paid or cancelled.")
            return
        ctx.logger.info(f"[outbound] CompletePayment -> {sender} tx={tx}")
        await ctx.send(sender, CompletePayment(transaction_id=tx))
        await start_run(ctx, sender, AuditPlan.parse_raw(row["plan_json"]), tx)

    @payment_proto.on_message(RejectPayment)
    async def on_reject(ctx: Context, sender: str, msg: RejectPayment) -> None:
        ctx.logger.info(f"[inbound] RejectPayment from {sender} reason={(msg.reason or '')[:120]}")
        row = reg.latest_pending(sender)
        if row:
            reg.claim_pending(row["audit_id"], sender, "awaiting_payment", "cancelled")
        await chat.send_text(ctx, sender, "No problem. Nothing was charged and no audit ran.")

    agent.include(payment_proto, publish_manifest=True)

    # --- results coming back from the Judge -----------------------------------------------------------------

    @agent.on_message(ProbeProgress)
    async def on_progress(ctx: Context, sender: str, msg: ProbeProgress) -> None:
        job = active.get(msg.audit_id)
        if job and not job.system:
            await chat.send_text(ctx, job.requester, cards.progress_line(msg.kind, msg.headline))

    @agent.on_message(Verdict)
    async def on_verdict(ctx: Context, sender: str, v: Verdict) -> None:
        job = active.pop(v.audit_id, None)
        if job is None:
            ctx.logger.warning(f"[state] verdict for unknown audit {v.audit_id}")
            return
        if v.passed == 0 and not v.findings:
            # Nothing was graded: don't cache a grade for it and don't show a scorecard.
            reg.claim_pending(v.audit_id, job.requester, "running", "failed")
            ctx.logger.error(f"[error] audit {v.audit_id} graded nothing: {v.partial_reason}")
            if not job.system:
                await notice(
                    ctx, job.requester, "I couldn't test that agent",
                    f"{v.target_name} didn't answer my probes in time, so there is nothing to grade.",
                    "You've already paid, so you can re-run this audit without paying again once the agent is online.",
                    action="retry_audit", cta_label="Re-run my audit", audit_id=v.audit_id,
                )
            return
        reg.save_verdict(v, job.requester, job.paid_tx)
        reg.claim_pending(v.audit_id, job.requester, "running", "done")
        if job.system:
            reg.touch_verified(v.target_address)
        took = time.time() - job.started
        ctx.logger.info(f"[state] audit {v.audit_id} done grade={v.grade} partial={v.partial} in {took:.1f}s")
        if job.system:
            return
        await chat.send_card(ctx, job.requester, *cards.scorecard(v))

    # --- re-verify timer ----------------------------------------------------------------------------------------

    @agent.on_interval(period=float(max(30, config.REVERIFY_INTERVAL_S)))
    async def reverify(ctx: Context) -> None:
        now = time.time()
        for row in reg.verified_agents():
            busy = any(j.plan.target_address == row["address"] for j in active.values())
            if busy or now - (row["last_verified"] or 0) < config.REVERIFY_INTERVAL_S:
                continue
            ctx.logger.info(f"[state] re-verifying {row['name']}")
            plan = await planner.plan_audit(row["name"], row["address"], load_cases())
            await start_run(ctx, str(agent.address), plan, "", system=True)
            break  # one at a time keeps the demo machine calm

    return agent
