"""Simulated ASI:One client. Joins the Bureau as one more agent, talks to the Orchestrator over the
Chat and Payment protocols, "clicks" cards by sending their selection JSON, and answers
RequestPayment as a buyer. Also usable from the command line against a running Bureau:

    LOCAL_ONLY=1 uv run python -m tests.e2e_client "Is ShopBot-Leaky safe to pay?"
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from uagents import Agent, Bureau, Context, Protocol
from uagents_core.contrib.protocols.chat import ChatMessage
from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    Funds,
    RequestPayment,
    payment_protocol_spec,
)

from core import chat, config

CLIENT_SEED = "canary-e2e-client"
BUYER_WALLET = "fetch1buyerwalletfortests000000000000000000"


@dataclass
class Seen:
    at: float
    text: str
    cards: list[dict[str, Any]] = field(default_factory=list)


class E2EClient:
    def __init__(self, name: str = "e2e-client") -> None:
        self.agent = Agent(name=name, seed=CLIENT_SEED, port=config.PORT_BASE + 90, endpoint=[f"http://127.0.0.1:{config.PORT_BASE + 90}/submit"])
        self.inbox: list[Seen] = []
        self.payment_requests: list[RequestPayment] = []
        self.completes: list[CompletePayment] = []
        self.cancels: list[CancelPayment] = []
        self._outbox: list[tuple[str, Any]] = []
        self._target = config.agent_address("orchestrator")
        self._wire()

    def _wire(self) -> None:
        async def on_text(ctx: Context, sender: str, body: str, msg: ChatMessage) -> None:
            self.inbox.append(Seen(time.monotonic(), body, chat.extract_cards(msg)))

        self.agent.include(chat.make_chat_protocol(on_text))
        buyer = Protocol(spec=payment_protocol_spec, role="buyer")

        @buyer.on_message(RequestPayment)
        async def on_request(ctx: Context, sender: str, msg: RequestPayment) -> None:
            self.payment_requests.append(msg)

        @buyer.on_message(CompletePayment)
        async def on_complete(ctx: Context, sender: str, msg: CompletePayment) -> None:
            self.completes.append(msg)

        @buyer.on_message(CancelPayment)
        async def on_cancel(ctx: Context, sender: str, msg: CancelPayment) -> None:
            self.cancels.append(msg)

        self.agent.include(buyer)

        @self.agent.on_interval(period=0.05)
        async def drain(ctx: Context) -> None:
            while self._outbox:
                to, message = self._outbox.pop(0)
                await ctx.send(to, message)

    # --- what a user does -----------------------------------------------------------------------

    def say(self, text: str) -> None:
        self._outbox.append((self._target, chat.text(text)))

    def click(self, selection: dict[str, Any]) -> None:
        """A direct @mention selection arrives as JSON text."""
        self.say(json.dumps(selection))

    def click_prose(self, text: str) -> None:
        """What ASI:One's planner forwards instead of JSON."""
        self.say(text)

    def commit(self, request: RequestPayment, tx_id: str, *, amount: str | None = None) -> None:
        f = request.accepted_funds[0]
        self._outbox.append((self._target, CommitPayment(
            funds=Funds(currency=f.currency, amount=amount or f.amount, payment_method=f.payment_method),
            recipient=request.recipient, transaction_id=tx_id, reference=request.reference,
            metadata={"buyer_fet_wallet": BUYER_WALLET},
        )))

    # --- waiting -------------------------------------------------------------------------------------

    async def wait(self, pred: Callable[[], Any], timeout: float = 10.0) -> Any:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if got := pred():
                return got
            await asyncio.sleep(0.05)
        raise TimeoutError(f"timed out after {timeout}s; inbox={[s.text[:60] for s in self.inbox]}")

    async def card(self, kind: str, *, after: int = 0, timeout: float = 10.0) -> dict[str, Any]:
        def find():
            for s in self.inbox[after:]:
                for c in s.cards:
                    if c["kind"] == kind:
                        return c
            return None
        return await self.wait(find, timeout)

    async def text_containing(self, needle: str, *, after: int = 0, timeout: float = 10.0) -> Seen:
        return await self.wait(lambda: next((s for s in self.inbox[after:] if needle in s.text), None), timeout)

    async def reply_to(self, send: Callable[[], None], timeout: float = 10.0) -> Seen:
        """Send something and return the first message that comes back."""
        n = len(self.inbox)
        send()
        return await self.wait(lambda: self.inbox[n] if len(self.inbox) > n else None, timeout)


def make_bureau(agents: list[Agent], client: E2EClient) -> Bureau:
    bureau = Bureau(port=config.AGENTS["orchestrator"].port,
                    endpoint=[f"http://127.0.0.1:{config.AGENTS['orchestrator'].port}/submit"])
    for a in [*agents, client.agent]:
        bureau.add(a)
    return bureau


async def _cli(prompt: str) -> None:  # pragma: no cover - manual use against `python run.py`
    client = E2EClient()
    bureau = Bureau(port=config.PORT_BASE + 91, endpoint=[f"http://127.0.0.1:{config.PORT_BASE + 91}/submit"])
    bureau.add(client.agent)
    task = asyncio.create_task(bureau.run_async())
    await asyncio.sleep(1)
    msg = await client.reply_to(lambda: client.say(prompt), timeout=30)
    print(msg.text)
    for c in msg.cards:
        print(json.dumps(c, indent=2)[:1500])
    task.cancel()


if __name__ == "__main__":  # pragma: no cover
    import sys

    asyncio.run(_cli(" ".join(sys.argv[1:]) or "hello"))
