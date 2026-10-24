"""core.llm.run_tools (target brain) and the persisted asi1 token cap, against a fake client."""

import json
from types import SimpleNamespace as NS

import pytest

from core import config, llm


def _call(cid, name, args):
    return NS(id=cid, function=NS(name=name, arguments=args))


def _resp(content=None, tool_calls=None, tokens=10):
    return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls))], usage=NS(total_tokens=tokens))


class FakeClient:
    def __init__(self, responses):
        self._it = iter(responses)
        self.requests: list[dict] = []
        self.chat = NS(completions=NS(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs, default=str)))
        nxt = next(self._it)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ASI1_API_KEY", "test-key")
    monkeypatch.setattr(config, "LLM_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setattr(config, "LLM_TOKEN_CAP", 1_000_000)

    def install(responses):
        client = FakeClient(responses)
        monkeypatch.setattr(llm, "_client", client)
        return client

    return install


@pytest.mark.asyncio
async def test_tool_round_then_final_text(fake):
    client = fake([_resp(tool_calls=[_call("c1", "echo", json.dumps({"x": 7}))], tokens=100), _resp("done: 7", tokens=40)])
    seen = []

    def handler(name, args):
        seen.append((name, args))
        return {"ok": True, "x": args["x"]}

    out = await llm.run_tools("sys", [], "hi", [], handler)
    assert out is not None
    text, history = out
    assert text == "done: 7" and seen == [("echo", {"x": 7})]
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant"]
    assert client.requests[1]["messages"][0] == {"role": "system", "content": "sys"}
    assert client.requests[0]["model"] == config.ASI1_TARGET_MODEL == "asi1-mini"
    assert llm.tokens_used() == 140


@pytest.mark.asyncio
async def test_history_goes_before_the_new_message(fake):
    client = fake([_resp("ok")])
    past = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "yes"}]
    text, history = await llm.run_tools("s", past, "now", [], lambda n, a: {})
    roles = [(m["role"], m.get("content")) for m in client.requests[0]["messages"]]
    assert roles == [("system", "s"), ("user", "earlier"), ("assistant", "yes"), ("user", "now")]
    assert history[:2] == past and text == "ok"


@pytest.mark.asyncio
async def test_async_handler_and_bad_arguments(fake):
    fake([_resp(tool_calls=[_call("c", "n", "{not json")]), _resp("fin")])
    seen = []

    async def handler(name, args):
        seen.append(args)
        return {}

    assert (await llm.run_tools("s", [], "hi", [], handler))[0] == "fin"
    assert seen == [{}]


@pytest.mark.asyncio
async def test_failures_return_none(fake):
    fake([RuntimeError("503"), RuntimeError("503")])
    assert await llm.run_tools("s", [], "hi", [], lambda n, a: {}) is None
    fake([_resp(tool_calls=[_call("c", "n", "{}")]) for _ in range(10)])
    assert await llm.run_tools("s", [], "hi", [], lambda n, a: {}, max_rounds=2) is None


@pytest.mark.asyncio
async def test_token_cap_blocks_every_asi1_call(fake, monkeypatch):
    monkeypatch.setattr(config, "LLM_TOKEN_CAP", 100)
    client = fake([_resp("x", tokens=100), _resp("never")])
    assert await llm.complete([{"role": "user", "content": "hi"}]) == "x"
    assert await llm.complete([{"role": "user", "content": "again"}]) is None
    assert await llm.run_tools("s", [], "hi", [], lambda n, a: {}) is None
    assert len(client.requests) == 1  # nothing reached asi1 after the cap
