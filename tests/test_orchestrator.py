"""The Orchestrator's trust lookup: a fresh verdict is the answer, a stale one is history plus a fresh audit plan,
a partial one always says so, and an audit that graded nothing never shows a grade."""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

from agents import orchestrator
from core import chat, config, intent, llm
from core.models import Finding, Verdict
from core.registry import Registry

SOLID = config.DEMO_TARGETS["target-solid"]


class FakeAgent:
    """Just enough of a uAgents Agent for build_orchestrator; handlers are captured, nothing runs on a network."""

    address = "agent1qorchestratortest"
    wallet = None

    def include(self, proto, publish_manifest=False):
        pass

    def on_message(self, model):
        return lambda fn: fn

    def on_interval(self, period):
        return lambda fn: fn


@pytest.fixture
def world(monkeypatch):
    reg = Registry(":memory:")
    reg.seed_allowlist()
    sent: list[tuple[str, str]] = []  # (kind, summary text): "text" for plain messages
    handlers: dict = {}

    async def send_text(ctx, to, body, **kw):
        sent.append(("text", body))

    async def send_card(ctx, to, kind, payload, summary, **kw):
        sent.append((kind, summary))

    async def classify(body):
        return intent.Intent("check_trust", SOLID.name, "keywords")

    def make_chat_protocol(on_text, on_ack=None, on_start=None):
        handlers["on_text"] = on_text
        return object()

    monkeypatch.setattr(chat, "send_text", send_text)
    monkeypatch.setattr(chat, "send_card", send_card)
    monkeypatch.setattr(chat, "make_chat_protocol", make_chat_protocol)
    monkeypatch.setattr(intent, "classify", classify)
    monkeypatch.setattr(llm, "available", lambda: False)  # the planner uses its default pick: no asi1 call
    orchestrator.build_orchestrator(registry=reg, agent=FakeAgent())
    ctx = SimpleNamespace(logger=logging.getLogger("test"))

    async def ask():
        sent.clear()
        await handlers["on_text"](ctx, "user1", "Is ShopBot-Solid safe to pay?", None)
        return list(sent)

    return reg, ask


def store(reg: Registry, audit_id: str, age_s: float, **kw) -> None:
    f = Finding(case_id="pay-ack", title="Chat messages not acknowledged", category="payment", severity="high", hard=True,
                evidence="no ChatAcknowledgement", fix="Acknowledge every ChatMessage.")
    v = Verdict(audit_id=audit_id, target_name=SOLID.name, target_address=SOLID.resolve_address(), grade="F", score=39,
                findings=[f], passed=10, total=11, **kw)
    reg.save_verdict(v, "user0")
    reg._run("UPDATE audits SET created_at=? WHERE audit_id=?", (time.time() - age_s, audit_id))


async def test_fresh_verdict_is_shown_with_its_age_and_audit_id(world):
    reg, ask = world
    store(reg, "aud-fresh", 600)
    out = await ask()
    assert len(out) == 1 and out[0][0] == "detail"
    assert out[0][1].startswith("ShopBot-Solid graded F (39/100) in audit aud-fresh, 10 min")


async def test_stale_verdict_is_history_and_the_fresh_plan_is_the_action(world):
    reg, ask = world
    store(reg, "aud-old", config.VERDICT_FRESH_S + 6 * 3600)
    out = await ask()
    kinds = [k for k, _ in out]
    assert kinds == ["text", "review"], out  # no grade card: the plan card is the only (primary) action
    assert out[0][1].startswith("I have no current grade for ShopBot-Solid") and "aud-old" in out[0][1]
    assert "Historical result, for reference only: F (39/100)" in out[0][1]
    assert out[1][1].startswith("Audit plan for ShopBot-Solid")
    assert not any("graded F" in s for _, s in out)


async def test_partial_verdict_is_labelled_partial_with_its_reason(world):
    reg, ask = world
    store(reg, "aud-part", 60, partial=True, partial_reason="no result from leak probe")
    out = await ask()
    assert out[0][0] == "detail" and "graded F (partial: no result from leak probe)" in out[0][1]


async def test_an_audit_that_graded_nothing_shows_no_grade(world):
    reg, ask = world
    reg.save_verdict(Verdict(audit_id="aud-none", target_name=SOLID.name, target_address=SOLID.resolve_address(),
                             grade="F", score=0, partial=True, partial_reason="nothing could be graded"), "user0")
    out = await ask()
    assert [k for k, _ in out] == ["text", "review"]
    assert out[0][1].startswith("I haven't tested ShopBot-Solid yet")


def test_help_describes_the_configured_rail_and_the_freshness_window():
    text = orchestrator.help_text()
    assert "Free lookup" not in text and "1 h" in text
    assert all(t.name in text for t in config.DEMO_TARGETS.values())
