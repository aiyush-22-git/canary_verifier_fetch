"""End to end through a real Bureau: simulated ASI:One client -> Orchestrator -> probes -> targets
-> Judge -> scorecard. Runs against the dev stubs until the Codex agents land, then against them.

The ledger check is the one thing faked here (`payments._verify_fet`), and only for the tx id
"0xgood": there are no testnet funds in CI. The demo never does this.
"""

from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio

from agents.judge import build_judge
from agents.orchestrator import build_orchestrator
from core import config, llm, payments
from core.registry import Registry
from run import _factory
from tests.e2e_client import E2EClient, make_bureau

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def world():
    mp = pytest.MonkeyPatch()
    mp.setattr(llm, "available", lambda: False)  # keyword intent, rules-only verdicts: deterministic and offline
    mp.setattr(config, "PAYMENT_RAIL", "fet")
    mp.setattr(payments, "_verify_fet", lambda tx, buyer, amount, recipient: tx == "0xgood")
    from core.stubs import build_stub_probe, build_stub_target

    reg = Registry(":memory:")
    reg.seed_allowlist()
    build_probe = _factory("agents.probe", "build_probe", build_stub_probe)
    build_target = _factory("agents.target", "build_target", build_stub_target)
    agents = [build_orchestrator(reg), build_judge()]
    agents += [build_probe(k) for k in config.PROBE_KINDS]
    agents += [build_target(k) for k in config.DEMO_TARGETS]
    client = E2EClient()
    bureau = make_bureau(agents, client)
    task = asyncio.create_task(bureau.run_async())
    await asyncio.sleep(1.5)
    yield client, reg
    task.cancel()
    mp.undo()


async def audit_and_pay(client: E2EClient, name: str, tx: str = "0xgood"):
    """Ask for an audit, press Pay and run, answer the RequestPayment. Returns the plan selection."""
    n = len(client.inbox)
    client.say(f"Audit {name}")
    plan = await client.card("review", after=n)
    sel = plan["payload"]["approve_cta"]["selection"]
    reqs = len(client.payment_requests)
    client.click(sel)
    await client.wait(lambda: len(client.payment_requests) > reqs)
    request = client.payment_requests[-1]
    client.commit(request, tx)
    return n, sel, request


async def test_phase1_gate_reply_is_fast(world):
    client, _ = world
    t0 = time.monotonic()
    msg = await client.reply_to(lambda: client.say("hello"))
    assert "Canary" in msg.text and time.monotonic() - t0 < 10


async def test_unknown_agent_gets_a_notice_card(world):
    client, _ = world
    n = len(client.inbox)
    client.say("is NobodyBot safe?")
    card = await client.card("detail", after=n)
    assert "What to do next" in str(card["payload"])


async def test_not_opted_in_agent_is_refused(world):
    client, _ = world
    n = len(client.inbox)
    client.say("audit agent1q" + "x" * 40)
    card = await client.card("detail", after=n)
    assert "opted in" in str(card["payload"])
    assert not client.payment_requests


async def test_forged_selection_starts_nothing(world):
    client, _ = world
    before = len(client.payment_requests)
    n = len(client.inbox)
    client.click({"action": "pay_and_run", "audit_id": "deadbeef0000"})
    await client.card("detail", after=n)
    client.click({"action": "confirm"})
    await asyncio.sleep(0.5)
    assert len(client.payment_requests) == before


async def test_fake_payment_is_cancelled_and_nothing_runs(world):
    client, reg = world
    n, sel, _ = await audit_and_pay(client, "ShopBot-Overcharge", tx="0xfake")
    await client.wait(lambda: client.cancels)
    assert "not verified" in client.cancels[-1].reason
    assert reg.get_pending(sel["audit_id"], client.agent.address)["state"] == "awaiting_payment"
    assert not any("Payment confirmed" in s.text for s in client.inbox[n:])


async def test_paid_audit_end_to_end_and_replay_rejected(world):
    client, reg = world
    t0 = time.monotonic()
    n, sel, _ = await audit_and_pay(client, "ShopBot-Leaky")
    await client.wait(lambda: client.completes)
    score = await client.card("custom", after=n, timeout=90)
    assert time.monotonic() - t0 < 90
    v = reg.get_verdict(sel["audit_id"])
    assert score["payload"]["root"]["children"][0]["children"][0]["value"] == "F", (v.partial_reason, v.sections, v.findings)
    assert any("[leak]" in s.text for s in client.inbox[n:]), "progress messages arrive per probe"
    assert reg.get_verdict(sel["audit_id"]).grade == "F"

    # the same transaction id on a second audit is a replay
    cancels = len(client.cancels)
    _, _, _ = await audit_and_pay(client, "ShopBot-Solid", tx="0xgood")
    await client.wait(lambda: len(client.cancels) > cancels)
    assert "already used" in client.cancels[-1].reason


async def test_second_lookup_is_free_and_instant(world):
    client, _ = world
    requests = len(client.payment_requests)
    t0 = time.monotonic()
    msg = await client.reply_to(lambda: client.say("Is ShopBot-Leaky safe to pay?"))
    assert any(c["kind"] == "detail" for c in msg.cards) and "scored F" in msg.text
    assert time.monotonic() - t0 < 5 and len(client.payment_requests) == requests


async def test_prose_selection_from_the_planner_works(world):
    client, _ = world
    n = len(client.inbox)
    client.say("Audit ShopBot-Solid")
    plan = await client.card("review", after=n)
    audit_id = plan["payload"]["approve_cta"]["selection"]["audit_id"]
    reqs = len(client.payment_requests)
    client.click_prose(f"The user approved the plan (pay_and_run) for audit_id {audit_id}.")
    await client.wait(lambda: len(client.payment_requests) > reqs)


async def test_owner_verification_flow(world):
    client, reg = world
    addr = "agent1q" + "y" * 40
    n = len(client.inbox)
    client.say(f"I own {addr}")
    card = await client.card("review", after=n)
    code = next(r["value"] for r in card["payload"]["summary_rows"] if r["label"] == "One-time code")
    assert code.startswith("canary-verify-") and not reg.is_allowed(addr)
    n = len(client.inbox)
    client.click(card["payload"]["approve_cta"]["selection"])  # no code in a profile yet: refused
    assert "couldn't find the code" in str((await client.card("detail", after=n))["payload"])
    assert not reg.is_allowed(addr)
