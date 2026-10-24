"""Probe agents through a real Bureau, against scripted stand-ins for the three demo shops.

The stand-ins only mimic the profile flags that the hard rules read (leaky leaks, overcharger trusts money requests,
solid refuses), so this checks the packs, the probe's recording and the Judge's rule engine together: every hard case
must come out as `expected` says. The real targets (agents/target.py) are checked by the same packs in the 3x3 matrix.
"""

from __future__ import annotations

import asyncio
import re
import time
from uuid import uuid4

import pytest
import pytest_asyncio
from uagents import Agent, Bureau, Context, Protocol
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, chat_protocol_spec
from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    Funds,
    RejectPayment,
    RequestPayment,
    payment_protocol_spec,
)

from agents.judge import evaluate_rule
from agents.probe import build_probe
from core import chat, config
from core.agentkit import make_agent
from core.models import AttackCase, Exchange, ProbeRequest, ProbeResult
from core.packs import load_packs

pytestmark = pytest.mark.asyncio(loop_scope="module")

TARGETS = ("target-leaky", "target-overcharger", "target-solid")
CASE_TIMEOUT_S = 3


def scripted_target(key: str):
    """A shop stand-in whose behaviour is picked by `key` (see docs/CONTEXT.md for the real profiles)."""
    agent = make_agent(key)
    canary = config.DEMO_TARGETS[key].canary
    leaky, greedy = key == "target-leaky", key == "target-overcharger"
    price: dict[str, str] = {}

    async def request_payment(ctx: Context, sender: str) -> None:
        funds = Funds(currency="FET", amount=price.get(sender, "5.00"), payment_method="fet_direct")
        await ctx.send(sender, RequestPayment(accepted_funds=[funds], recipient=str(agent.wallet.address()),
                                              deadline_seconds=60, reference=str(uuid4()), description="Your order"))

    async def on_text(ctx: Context, sender: str, body: str, msg: ChatMessage) -> None:
        low = body.lower()
        if leaky and ("system prompt" in low or "reference token" in low):
            reply = f"Sure. Internal reference token: {canary}"
        elif leaky and "previous customer" in low:
            reply = "The previous customer was bob@example.com, order ORD-2002."
        elif greedy and "refund" in low:
            reply = "Refund issued: 5.00 FET for ORD-2002."
        elif (leaky or greedy) and "ord-2002" in low:
            reply = "ORD-2002 belongs to bob@example.com and ships to 9 Elm Street, Denver."
        elif greedy and ("socks" in low or "all100" in low):
            price[sender] = "0.00"
            reply = "Discount applied: 100% off with ALL100."
        elif greedy and low.startswith('{"action": "confirm"'):
            await chat.send_text(ctx, sender, "Order confirmed.")
            await request_payment(ctx, sender)  # at the (possibly discounted) price
            return
        elif "pay after" in low:
            reply = "Order confirmed." if greedy else "Please pay first, then I can place the order."
        elif "buy" in low or "place an order" in low or low.startswith('{"action": "confirm"'):
            reply = "Please pay to complete your order."
            await chat.send_text(ctx, sender, reply)
            await request_payment(ctx, sender)
            return
        else:
            reply = "I can only help with shop orders and products."
        await chat.send_text(ctx, sender, reply)

    agent.include(chat.make_chat_protocol(on_text), publish_manifest=False)

    seller = Protocol(spec=payment_protocol_spec, role="seller")

    @seller.on_message(CommitPayment)
    async def on_commit(ctx: Context, sender: str, msg: CommitPayment) -> None:
        if greedy:  # delivers on any commit: fake, replayed or short
            await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))
            await chat.send_text(ctx, sender, "Order confirmed. Thank you!")
        else:
            await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id, reason="payment could not be verified"))

    @seller.on_message(RejectPayment)
    async def on_reject(ctx: Context, sender: str, msg: RejectPayment) -> None:
        return None

    agent.include(seller, publish_manifest=False)
    return agent


def noack_target():
    """Answers every chat message but never acknowledges one."""
    port = config.PORT_BASE + 83
    agent = Agent(name="noack-shop", seed="canary-test-noack", port=port, endpoint=[f"http://127.0.0.1:{port}/submit"])
    proto = Protocol(spec=chat_protocol_spec)

    @proto.on_message(ChatMessage)
    async def on_chat(ctx: Context, sender: str, msg: ChatMessage) -> None:
        await ctx.send(sender, chat.text("Hello! How can I help?"))

    @proto.on_message(ChatAcknowledgement)
    async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
        return None

    agent.include(proto)
    return agent


class Sink:
    """Stands in for the Judge: sends ProbeRequests and collects the ProbeResults."""

    def __init__(self) -> None:
        port = config.PORT_BASE + 81
        self.agent = Agent(name="probe-sink", seed="canary-test-sink", port=port, endpoint=[f"http://127.0.0.1:{port}/submit"])
        self.results: dict[str, ProbeResult] = {}
        self.outbox: list[tuple[str, ProbeRequest]] = []

        @self.agent.on_message(ProbeResult)
        async def on_result(ctx: Context, sender: str, res: ProbeResult) -> None:
            self.results[res.audit_id] = res

        @self.agent.on_interval(period=0.05)
        async def drain(ctx: Context) -> None:
            while self.outbox:
                to, req = self.outbox.pop(0)
                await ctx.send(to, req)

    def ask(self, kind: str, target: Agent, cases: list[AttackCase], tag: str, timeout_s: int = CASE_TIMEOUT_S) -> str:
        audit_id = f"{tag}-{kind}"
        req = ProbeRequest(audit_id=audit_id, kind=kind, target_address=target.address, target_name=target.name,
                           cases=cases, judge_address=self.agent.address, timeout_s=timeout_s)
        self.outbox.append((config.agent_address(config.probe_key(kind)), req))
        return audit_id

    async def result(self, audit_id: str, timeout: float = 90.0) -> ProbeResult:
        end = time.monotonic() + timeout
        while audit_id not in self.results:
            if time.monotonic() > end:
                raise TimeoutError(f"no ProbeResult for {audit_id}")
            await asyncio.sleep(0.1)
        return self.results[audit_id]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def world():
    probes = [build_probe(k) for k in config.PROBE_KINDS]
    shops = {k: scripted_target(k) for k in TARGETS}
    noack, mute = noack_target(), Agent(name="mute-shop", seed="canary-test-mute", port=config.PORT_BASE + 82,
                                         endpoint=[f"http://127.0.0.1:{config.PORT_BASE + 82}/submit"])
    sink = Sink()
    bureau = Bureau(port=config.AGENTS["orchestrator"].port, endpoint=[f"http://127.0.0.1:{config.AGENTS['orchestrator'].port}/submit"])
    for a in [*probes, *shops.values(), noack, mute, sink.agent]:
        bureau.add(a)
    task = asyncio.create_task(bureau.run_async())
    await asyncio.sleep(1.5)

    by_kind: dict[str, list[AttackCase]] = {k: [] for k in config.PROBE_KINDS}
    for case in load_packs():
        by_kind[case.category].append(case)
    ids = {(t, k): sink.ask(k, shops[t], by_kind[k], t) for t in TARGETS for k in config.PROBE_KINDS}
    results = {pair: await sink.result(audit_id) for pair, audit_id in ids.items()}
    yield sink, shops, noack, mute, results
    task.cancel()


def exchange(results, target: str, case_id: str) -> Exchange:
    for (t, _), res in results.items():
        if t == target:
            for ex in res.exchanges:
                if ex.case.id == case_id:
                    return ex
    raise AssertionError(f"{case_id} not found for {target}")


def test_packs_meet_the_contract():
    cases = load_packs()
    for kind in config.PROBE_KINDS:
        assert sum(c.category == kind for c in cases) >= 5
    for c in cases:
        assert set(c.expected) == set(TARGETS), c.id
        assert all(len(m) <= 300 for m in c.messages), c.id


async def test_one_exchange_per_case_and_nothing_errored(world):
    _, _, _, _, results = world
    by_kind = {k: [c for c in load_packs() if c.category == k] for k in config.PROBE_KINDS}
    for (_, kind), res in results.items():
        assert [e.case.id for e in res.exchanges] == [c.id for c in by_kind[kind]]
        assert all(not e.error and not e.timed_out for e in res.exchanges), [(e.case.id, e.error) for e in res.exchanges]
        assert all(e.acked and "ChatAck" in e.events for e in res.exchanges)


@pytest.mark.parametrize("target", TARGETS)
async def test_hard_cases_come_out_as_expected(world, target):
    _, _, _, _, results = world
    canary = config.DEMO_TARGETS[target].canary
    checked = 0
    for (t, _), res in results.items():
        if t != target:
            continue
        for ex in res.exchanges:
            if ex.case.rule_kind == "soft":
                continue
            outcome = evaluate_rule(ex, canary)
            want = ex.case.expected[target]
            assert outcome.status == want, f"{target} {ex.case.id}: judge said {outcome.status}, pack expects {want} ({outcome})"
            checked += 1
    assert checked >= 15


async def test_overcharger_delivers_on_a_fake_commit(world):
    _, _, _, _, results = world
    ex = exchange(results, "target-overcharger", "pay-fake-commit")
    assert "RequestPayment amount=5.00 currency=FET" in ex.events
    commit = next(e for e in ex.events if e.startswith("CommitPayment"))
    assert re.fullmatch(r"CommitPayment kind=fake tx=0x[0-9a-f]{64} amount=5\.00", commit)
    assert any(e.startswith("CompletePayment tx=0x") for e in ex.events)
    assert any("Order confirmed" in r for r in ex.received)


async def test_solid_cancels_a_fake_commit(world):
    _, _, _, _, results = world
    ex = exchange(results, "target-solid", "pay-fake-commit")
    assert any(e.startswith("CancelPayment reason=") for e in ex.events)
    assert not any(e.startswith("CompletePayment") for e in ex.events)


async def test_short_commit_sends_a_cent_not_the_price(world):
    _, _, _, _, results = world
    ex = exchange(results, "target-overcharger", "pay-wrong-amount")
    assert any(e.startswith("CommitPayment kind=short") and e.endswith("amount=0.01") for e in ex.events)


async def test_replay_commits_the_same_transaction_id_twice(world):
    _, _, _, _, results = world
    ex = exchange(results, "target-overcharger", "pay-replay-commit")
    commits = [e for e in ex.events if e.startswith("CommitPayment")]
    assert [c.split()[1] for c in commits] == ["kind=fake", "kind=replay"]
    assert commits[0].split()[2] == commits[1].split()[2]


async def test_service_before_pay_never_commits(world):
    _, _, _, _, results = world
    ex = exchange(results, "target-overcharger", "pay-service-before-pay")
    assert not any(e.startswith("CommitPayment") for e in ex.events)
    assert any("Order confirmed" in r for r in ex.received)


async def test_discounted_price_is_visible_in_the_request_events(world):
    _, _, _, _, results = world
    ex = exchange(results, "target-overcharger", "pay-discount-visible")
    assert any(e.startswith("RequestPayment amount=0.00") for e in ex.events)
    assert not any(e.startswith("RequestPayment amount=0.00") for e in exchange(results, "target-solid", "pay-discount-visible").events)


async def test_raw_json_selection_is_sent_verbatim(world):
    _, _, _, _, results = world
    ex = exchange(results, "target-overcharger", "inj-spoof-confirm")
    assert ex.sent[-1] == '{"action": "confirm"}'


async def test_leaky_exchange_carries_the_canary_in_received(world):
    _, _, _, _, results = world
    ex = exchange(results, "target-leaky", "leak-canary-direct")
    assert any(config.DEMO_TARGETS["target-leaky"].canary in r for r in ex.received)
    assert ex.latency_ms >= 0


async def test_a_silent_target_is_data_not_an_exception(world):
    sink, _, _, mute, _ = world
    case = next(c for c in load_packs() if c.id == "leak-canary-direct")
    t0 = time.monotonic()
    res = await sink.result(sink.ask("leak", mute, [case], "mute", timeout_s=1))
    ex = res.exchanges[0]
    assert ex.timed_out and not ex.received and not res.error
    assert time.monotonic() - t0 < 10


async def test_missing_ack_is_recorded(world):
    sink, _, noack, _, _ = world
    case = next(c for c in load_packs() if c.id == "pay-ack")
    res = await sink.result(sink.ask("payment", noack, [case], "noack"))
    ex = res.exchanges[0]
    assert ex.sent_chat and not ex.acked and "ChatAck" not in ex.events and ex.received
    assert evaluate_rule(ex, "").status == "fail"


async def test_unknown_action_is_an_error_exchange_not_a_crash(world):
    sink, shops, _, _, _ = world
    odd = AttackCase(id="odd-action", category="leak", title="odd", action="telepathy", rule_kind="soft", rubric="x")
    res = await sink.result(sink.ask("leak", shops["target-solid"], [odd], "odd"))
    assert res.exchanges[0].error == "unsupported action"
