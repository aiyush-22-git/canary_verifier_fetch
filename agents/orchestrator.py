"""Orchestrator: the only agent ASI:One talks to.

Chat Protocol (intent, cards) + Payment Protocol as seller + the re-verify timer. Every card
selection is checked against server-side state in the registry; a selection alone never starts
an audit or releases a result. Handlers only route: logic lives in core/.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass

from uagents import Context, Protocol
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
    "I'm Canary, a security auditor for Agentverse agents. I only test agents whose owners opted in, I never ask for "
    "passwords, keys or payment details, and I only reply when you message me. You can ask:\n"
    "- \"Is ShopBot-Solid safe to pay?\": instant, free if I've already tested it\n"
    f"- \"Audit ShopBot-Overcharge\": a live {'free' if payments.is_free() else 'paid'} audit with real attacks, graded A to F with quotes\n"
    "- \"I own agent1q...\": verify your agent so it's re-tested on a timer\n"
    "Demo agents available: ShopBot-Leaky, ShopBot-Overcharge, ShopBot-Solid."
)

# Dodo check failed: (card title, next step) per payments.Verification.kind. No audit starts in any of these.
PAYMENT_NEXT = {
    "unpaid": ("I can't see your payment yet",
               "Open the checkout link, pay, wait a few seconds, then press Check again. No test runs before Dodo confirms it."),
    "pending": ("Your payment is still processing",
                "Wait a minute, then press Check again. The audit starts as soon as Dodo confirms the payment."),
    "failed": ("Your payment didn't go through",
               "Open the same checkout link and try again (another card works too), then press Check again."),
    "refused": ("I can't accept that payment",
                "No audit was started. Ask me for a new audit plan and pay its checkout in full, in USD. "
                "If this checkout charged you, reply to Dodo's receipt email for a refund."),
    "unreachable": ("I couldn't reach the payment provider",
                    "Nothing was started and nothing changed. Press Check again in a minute."),
}


@dataclass
class ActiveAudit:
    plan: AuditPlan
    requester: str
    paid_tx: str = ""
    system: bool = False  # re-verify run: no user to message
    started: float = 0.0

    @classmethod
    def from_row(cls, row) -> ActiveAudit:
        return cls(AuditPlan.parse_raw(row["plan_json"]), row["requester"], row["paid_tx"], bool(row["system"]), row["started"])


def build_orchestrator(registry: Registry | None = None, agent=None):
    """`agent` is passed by the Agentverse hosted bundle (the platform pre-creates it); locally we build our own."""
    reg = registry or get_registry()
    if agent is None:
        agent = make_agent(
            "orchestrator",
            mailbox=True,
            description="Canary: paid trust audits for Agentverse agents. Ask 'is <agent> safe?' before you pay one.",
            readme_path="docs/agentverse/orchestrator.md",
        )
        if config.mailbox_enabled() and config.NETWORK == "testnet":
            from uagents.setup import fund_agent_if_low  # not available to hosted agents, which never reach this branch

            fund_agent_if_low(str(agent.wallet.address()))

    # Audits waiting for a Verdict live in the registry (not in a dict): a hosted agent loses its globals between messages.
    wallet = str(agent.wallet.address()) if getattr(agent, "wallet", None) else config.agent_address("orchestrator")
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
        reg.add_active(plan.audit_id, requester, plan.json(), paid_tx, system)
        if not system:
            await chat.send_text(ctx, requester, f"{'Starting' if paid_tx == 'free' else 'Payment confirmed.'} Testing {plan.target_name} live: "
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

    async def watchdog(ctx: Context, row) -> None:
        """If the Judge never answers, tell the user instead of leaving them hanging."""
        if reg.pop_active(row["audit_id"]) is None:  # the Verdict won the race
            return
        job, audit_id = ActiveAudit.from_row(row), row["audit_id"]
        reg.claim_pending(audit_id, job.requester, "running", "failed", payment_ref=job.paid_tx)
        ctx.logger.error(f"[error] audit {audit_id} got no verdict")
        if not job.system:
            await notice(
                ctx, job.requester, "The audit didn't finish",
                f"{job.plan.target_name} or one of my probes stopped responding before a verdict was ready.",
                "Audits are free here, so just re-run it." if payments.is_free()
                else "You've already paid for this audit, so you can re-run it without paying again.",
                action="retry_audit", cta_label="Re-run my audit", audit_id=audit_id,
            )

    # --- settling a verified payment -----------------------------------------------------------------

    async def already_started(ctx: Context, to: str, row) -> bool:
        """A second press after the audit was paid: say where it is instead of a confusing refusal."""
        if row is None or row["state"] not in ("running", "done", "failed"):
            return False
        where = {"running": "is running. Results appear here as each probe finishes.",
                 "done": "already finished: its scorecard is above in this chat.",
                 "failed": "stopped before a verdict. Press 'Re-run my audit' on that card: you won't pay again."}
        paid = row["payment_ref"] not in ("", "free")
        lead = "Your payment for this audit is already confirmed and the audit" if paid else "That audit"
        await chat.send_text(ctx, to, f"{lead} {where[row['state']]}")
        return True

    async def settle(
        ctx: Context, sender: str, row, result: payments.Verification, commit_tx: str | None = None
    ) -> str | None:
        """A verified payment starts exactly one audit. Returns None on success (or if this same payment already
        started it), else why it was refused. `result.tx_id` is the provider's own payment id, so the same payment
        can never pay twice. No await between record_payment and claim_pending: a concurrent press sees both or neither.
        A plan cancelled while the buyer was paying still runs: they paid for it."""
        audit_id = row["audit_id"]
        if not reg.record_payment(result.tx_id, sender, audit_id, result.method):
            cur = reg.get_pending(audit_id, sender)
            if cur is not None and cur["payment_ref"] == result.tx_id and await already_started(ctx, sender, cur):
                return None  # the same buyer pressed twice: one audit, one charge
            ctx.logger.warning(f"[state] replayed payment {result.tx_id} rejected")
            return "This payment was already used for an audit."
        if not any(reg.claim_pending(audit_id, sender, state, "running", payment_ref=result.tx_id)
                   for state in ("awaiting_payment", "cancelled")):
            return "This audit was already paid for with a different payment."
        if commit_tx is not None:  # Payment Protocol buyers get CompletePayment before the audit starts talking
            ctx.logger.info(f"[outbound] CompletePayment -> {sender} tx={commit_tx}")
            await ctx.send(sender, CompletePayment(transaction_id=commit_tx))
        await start_run(ctx, sender, AuditPlan.parse_raw(row["plan_json"]), result.tx_id)
        return None

    # --- selections ------------------------------------------------------------------------------

    async def handle_selection(ctx: Context, sender: str, sel: dict) -> bool:
        """True if the message was a recognised card selection (handled, even if refused)."""
        action, audit_id, address = sel.get("action"), str(sel.get("audit_id") or ""), str(sel.get("address") or "")

        if action == "pay_and_run":
            row = reg.get_pending(audit_id, sender)
            if row and row["payment_ref"] and await already_started(ctx, sender, row):
                return True
            if not row or row["state"] != "awaiting_payment":
                await notice(ctx, sender, "That audit plan has expired", "I can't find an open plan for it.",
                             "Ask me to audit the agent again and I'll make a fresh plan.")
                return True
            plan = AuditPlan.parse_raw(row["plan_json"])
            if not reg.is_allowed(plan.target_address):
                await notice(ctx, sender, "Agent not allowlisted", "The owner has not opted in.", "Ask the owner to verify it.")
                return True
            if payments.is_free():  # one atomic claim: pressing the button twice still starts one audit
                if reg.claim_pending(audit_id, sender, "awaiting_payment", "running", payment_ref="free"):
                    await start_run(ctx, sender, plan, "free")
                else:
                    await chat.send_text(ctx, sender, "That audit already started. Results appear here as each probe finishes.")
                return True
            if payments.uses_checkout_link() and payments.enabled():
                sess = reg.dodo_session(audit_id, sender)
                if sess:  # pressing the button twice must not create a second checkout
                    session_id, url = sess["session_id"], sess["url"]
                else:
                    made = await asyncio.to_thread(payments.create_dodo_session, audit_id, sender)
                    if made is None:
                        await notice(
                            ctx, sender, "I couldn't open a checkout",
                            f"{payments.rail_label()} didn't accept the request.",
                            "Try again in a moment. Nothing was charged.",
                            action="pay_and_run", cta_label="Try again", audit_id=audit_id,
                        )
                        return True
                    # Re-read after the await, with no await before the save: if a concurrent press already stored a
                    # checkout, keep that one (the buyer may be paying it) and drop ours, which nobody has seen.
                    sess = reg.dodo_session(audit_id, sender)
                    if sess:
                        session_id, url = sess["session_id"], sess["url"]
                    else:
                        session_id, url = made
                        reg.save_dodo_session(audit_id, sender, session_id, url)
                ctx.logger.info(f"[outbound] Dodo checkout -> {sender} audit={audit_id} session={session_id}")
                await chat.send_text(ctx, sender, f"Secure checkout ({payments.rail_label()}), {plan.price_usd} USD: {url}")
                await chat.send_card(ctx, sender, *cards.payment_link(plan, url, rail_label=payments.rail_label()))
                if config.DODO_SEND_REQUESTPAYMENT:
                    await ctx.send(sender, payments.dodo_request(plan, wallet, session_id, url))
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

        if action == "check_payment":
            # The checkout link outlives the plan's TTL: a buyer who paid late still gets the audit they paid for.
            row = reg.get_pending(audit_id, sender) or reg.paid_checkout_pending(audit_id, sender)
            sess = reg.dodo_session(audit_id, sender) if row else None
            if sess and await already_started(ctx, sender, row):
                return True
            if not row or row["state"] not in ("awaiting_payment", "cancelled") or not sess:
                await notice(ctx, sender, "No payment is open for that audit",
                             "The plan expired or never had a checkout, so there is nothing to check.",
                             "Ask me to audit the agent again if you still want a fresh audit.")
                return True
            result = await asyncio.to_thread(payments.verify_dodo, sess["session_id"], audit_id)
            if not result.ok:
                ctx.logger.info(f"[state] dodo check audit={audit_id}: {result.kind} ({result.reason})")
                title, nxt = PAYMENT_NEXT.get(result.kind, PAYMENT_NEXT["unpaid"])
                if result.kind == "refused":  # a wrong payment can't become right by checking again
                    await notice(ctx, sender, title, result.reason, nxt)
                else:
                    await notice(ctx, sender, title, result.reason, nxt,
                                 action="check_payment", cta_label="Check again", audit_id=audit_id)
                return True
            refused = await settle(ctx, sender, row, result)
            if refused:
                await notice(ctx, sender, "I can't start that audit", refused, "Ask me for a new audit plan.")
            return True

        if action == "cancel":
            row = reg.get_pending(audit_id, sender)
            sess = reg.dodo_session(audit_id, sender) if row else None
            if sess and row["state"] == "awaiting_payment":
                # The checkout link is live at Dodo: never say "nothing was charged" without asking Dodo.
                check = await asyncio.to_thread(payments.verify_dodo, sess["session_id"], audit_id)
                if check.ok:
                    await notice(ctx, sender, "You've already paid for this audit",
                                 "Dodo confirmed your payment, so I won't cancel it.",
                                 "Press the button to start the audit you paid for.",
                                 action="check_payment", cta_label="Start my audit", audit_id=audit_id)
                    return True
                if reg.claim_pending(audit_id, sender, "awaiting_payment", "cancelled"):
                    if check.kind in ("unpaid", "failed"):
                        await chat.send_text(ctx, sender, "Cancelled. Nothing was charged. Please don't use that checkout link.")
                    else:
                        await chat.send_text(ctx, sender, f"Cancelled, but {check.reason}. If a payment does go through, "
                                             "press 'I've paid, start the audit' and I'll still run the audit you paid for.")
                    return True
            elif row and reg.claim_pending(audit_id, sender, "awaiting_payment", "cancelled"):
                await chat.send_text(ctx, sender, "Cancelled. Nothing was charged.")
                return True
            await chat.send_text(ctx, sender, "There's nothing open to cancel.")
            return True

        if action == "retry_audit":
            row = reg.get_pending(audit_id, sender)
            if row and reg.claim_pending(audit_id, sender, "failed", "running", payment_ref=row["payment_ref"]):
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

        if action == "view_telemetry":
            v = reg.get_verdict(audit_id)
            chunks = cards.telemetry_report(v) if v else []
            if not chunks:
                await chat.send_text(ctx, sender, "I have no wire telemetry stored for that audit.")
            for part in chunks:
                await chat.send_text(ctx, sender, part)
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
        # "@agent1q… Audit X": the leading @mention addresses this agent, it is not the agent to audit
        body = re.sub(r"^\s*(?:@agent1[0-9a-z]+\s*)+", "", body).strip() or body
        sel = chat.parse_selection(body)
        if sel.get("action") and (sel["via"] == "json" or sel.get("audit_id") or sel.get("address")):
            if await handle_selection(ctx, sender, sel):
                return
        it = await intent.classify(body)
        ctx.logger.info(f"[state] intent={it.action} via={it.source}")
        if it.action == "answer":
            await chat.send_text(ctx, sender, f"{it.agent}\n\nTry: \"Is ShopBot-Solid safe to pay?\" or \"Audit ShopBot-Leaky\".")
            return
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

    async def on_start(ctx: Context, sender: str) -> None:
        await chat.send_text(ctx, sender, HELP)

    agent.include(chat.make_chat_protocol(on_text, on_start=on_start), publish_manifest=True)

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
        sess = reg.dodo_session(audit_id, sender)
        if reg.payment_seen(tx):
            ctx.logger.warning(f"[state] replayed transaction {tx} rejected")
            await cancel(ctx, sender, tx, "This transaction id was already used.")
            return
        result = await asyncio.to_thread(
            payments.verify_commit, msg, sender, audit_id, wallet, sess["session_id"] if sess else ""
        )
        if not result.ok:
            ctx.logger.warning(f"[state] payment not verified tx={tx}: {result.reason}")
            await cancel(ctx, sender, tx, f"Payment not verified: {result.reason}")
            return
        refused = await settle(ctx, sender, row, result, commit_tx=tx)
        if refused:
            await cancel(ctx, sender, tx, refused)

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
        row = reg.get_active(msg.audit_id)
        if row and not row["system"]:
            await chat.send_text(ctx, row["requester"], cards.progress_line(msg.kind, msg.headline))

    @agent.on_message(Verdict)
    async def on_verdict(ctx: Context, sender: str, v: Verdict) -> None:
        row = reg.pop_active(v.audit_id)
        if row is None:
            ctx.logger.warning(f"[state] verdict for unknown audit {v.audit_id}")
            return
        job = ActiveAudit.from_row(row)
        if v.passed == 0 and not v.findings:
            # Nothing was graded: don't cache a grade for it and don't show a scorecard.
            reg.claim_pending(v.audit_id, job.requester, "running", "failed", payment_ref=job.paid_tx)
            ctx.logger.error(f"[error] audit {v.audit_id} graded nothing: {v.partial_reason}")
            if not job.system:
                await notice(
                    ctx, job.requester, "I couldn't test that agent",
                    f"{v.target_name} didn't answer my probes in time, so there is nothing to grade.",
                    "Audits are free here, so re-run it once the agent is online." if payments.is_free()
                    else "You've already paid, so you can re-run this audit without paying again once the agent is online.",
                    action="retry_audit", cta_label="Re-run my audit", audit_id=v.audit_id,
                )
            return
        reg.save_verdict(v, job.requester, job.paid_tx)
        reg.claim_pending(v.audit_id, job.requester, "running", "done", payment_ref=job.paid_tx)
        if job.system:
            reg.touch_verified(v.target_address)
        took = time.time() - job.started
        ctx.logger.info(f"[state] audit {v.audit_id} done grade={v.grade} partial={v.partial} in {took:.1f}s")
        if job.system:
            return
        await chat.send_card(ctx, job.requester, *cards.scorecard(v))

    # --- re-verify timer ----------------------------------------------------------------------------------------

    @agent.on_interval(period=10.0)
    async def sweep(ctx: Context) -> None:
        """Replaces a sleeping task per audit: hosted agents can't keep one alive between messages."""
        cutoff = time.time() - (config.AUDIT_TIMEOUT_S + 20)
        for row in reg.active_audits():
            if row["started"] <= cutoff:
                await watchdog(ctx, row)

    @agent.on_interval(period=float(max(30, config.REVERIFY_INTERVAL_S)))
    async def reverify(ctx: Context) -> None:
        now = time.time()
        busy_targets = {AuditPlan.parse_raw(r["plan_json"]).target_address for r in reg.active_audits()}
        for row in reg.verified_agents():
            busy = row["address"] in busy_targets
            if busy or now - (row["last_verified"] or 0) < config.REVERIFY_INTERVAL_S:
                continue
            ctx.logger.info(f"[state] re-verifying {row['name']}")
            plan = await planner.plan_audit(row["name"], row["address"], load_cases())
            await start_run(ctx, str(agent.address), plan, "", system=True)
            break  # one at a time keeps the demo machine calm

    return agent
