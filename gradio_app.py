"""Canary web chat: a Gradio UI that talks to the Canary Orchestrator over the Agentverse Chat Protocol.

    uv run python gradio_app.py                    # chat with the hosted Orchestrator on Agentverse
    CANARY_WEB_MODE=local CANARY_PORT_BASE=8500 LOCAL_ONLY=1 uv run python gradio_app.py   # against a local `run.py`

The page is a normal agent ("Canary-Web", own seed) that sends your messages to the Orchestrator and shows what comes back:
text, cards as readable markdown, and each card's buttons as real buttons. A reply from a hosted agent needs somewhere to be
delivered, so in the default mode this agent has an Agentverse mailbox:
  - with AGENTVERSE_API_KEY in .env it registers itself (nothing to click), or
  - without it, open the inspector link it prints, choose Connect > Mailbox once, and restart.
One browser conversation at a time: all pages share this agent's inbox.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

import gradio as gr
from uagents import Agent, Context

from core import chat, config

TARGET = os.environ.get("CANARY_TARGET") or "agent1qw4vnrljtcgkfnxwj3e4tlvhvxrp7jjxr4thulgkrs7kesmv6gd2way6kd6"
MODE = os.environ.get("CANARY_WEB_MODE", "mailbox")  # mailbox: hosted Orchestrator | local: a local run.py Bureau
PORT = int(os.environ.get("CANARY_WEB_AGENT_PORT", "8590"))
FINAL = re.compile(r"graded [A-F]|couldn't test|didn't finish|can't start that audit|expired", re.I)
RUNNING = re.compile(r"Testing .* live|Starting\. Testing|already started", re.I)
MAX_BUTTONS = 8


# --- card rendering -------------------------------------------------------------------------------


def _walk_actions(node: Any, out: list[tuple[str, dict]], label: str = "") -> None:
    if isinstance(node, dict):
        here = str(node.get("label") or label)
        sel = node.get("selection")
        if not isinstance(sel, dict):
            action = node.get("action")
            sel = action.get("selection") if isinstance(action, dict) else None
        if isinstance(sel, dict) and sel.get("action") and here and not any(s == sel for _, s in out):
            out.append((here, sel))
        for v in node.values():
            _walk_actions(v, out, here)
    elif isinstance(node, list):
        for v in node:
            _walk_actions(v, out, label)


def _flatten(node: Any, lines: list[str]) -> None:
    """Readable text from a card's element tree, in order."""
    if isinstance(node, dict):
        t = node.get("type")
        if t == "heading" and node.get("value"):
            lines.append(f"\n**{node['value']}**")
        elif t == "text" and node.get("value"):
            lines.append(str(node["value"]))
        elif t == "badge" and node.get("label"):
            lines.append(f"`{node['label']}`")
        elif t == "list":
            for it in node.get("items", []):
                lines.append("- " + " ".join(_text_of(it)))
            return
        for key in ("title", "subtitle"):
            if node.get(key) and t is None:
                lines.append(f"**{node[key]}**" if key == "title" else str(node[key]))
        for row in node.get("summary_rows", []) or []:
            lines.append(f"- **{row.get('label')}:** {row.get('value')}")
        for v in node.values():
            if isinstance(v, (dict, list)):
                _flatten(v, lines)
    elif isinstance(node, list):
        for v in node:
            _flatten(v, lines)


def _text_of(node: Any) -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        for k in ("title", "label", "value", "text", "subtitle"):
            if node.get(k):
                out.append(str(node[k]))
        for v in node.values():
            if isinstance(v, (dict, list)):
                out += _text_of(v)
    elif isinstance(node, list):
        for v in node:
            out += _text_of(v)
    elif node:
        out.append(str(node))
    return out


def render_message(text: str, cards: list[dict]) -> tuple[str, list[tuple[str, dict]]]:
    """(markdown, buttons) for one incoming message."""
    parts, buttons = [text.strip()] if text.strip() else [], []
    for c in cards:
        lines: list[str] = []
        payload = c["payload"]
        _flatten(payload, lines)
        if payload.get("title") and c["kind"] != "custom":
            lines = [line for line in lines if line.strip("* ") != str(payload["title"])]  # the title appears once, as a heading
            lines.insert(0, f"### {payload['title']}")
        body = "\n".join(dict.fromkeys(line for line in lines if line.strip()))  # drop repeated lines
        if body and body.strip() != text.strip():
            parts.append(body)
        found: list[tuple[str, dict]] = []
        _walk_actions(payload, found)
        title = str(payload.get("title") or "")
        for label, sel in found:  # name the target on the button, so a button on an older plan can't be mistaken for the newest
            buttons.append((f"{label} ({title})" if title and title not in label else label, sel))
    return "\n\n".join(parts), buttons


# --- the agent that talks to Agentverse -----------------------------------------------------------


def _web_seed() -> str:
    if os.environ.get("CANARY_WEB_SEED"):
        return os.environ["CANARY_WEB_SEED"]
    path = Path.home() / ".canary" / "web_seed"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("canary-web-" + secrets.token_hex(16))
        path.chmod(0o600)
    return path.read_text().strip()


def _register_mailbox(seed: str) -> str:
    """Register this agent's mailbox through the Agentverse API (needs AGENTVERSE_API_KEY). Returns a status line."""
    from uagents_core.config import AgentverseConfig
    from uagents_core.contrib.protocols.chat import chat_protocol_spec
    from uagents_core.protocol import ProtocolSpecification
    from uagents_core.utils.registration import (
        AgentverseRegistrationRequest,
        RegistrationRequestCredentials,
        register_agent,
    )

    cfg = AgentverseConfig()
    req = AgentverseRegistrationRequest(
        name="Canary-Web", endpoint=cfg.mailbox_endpoint, type="mailbox", active=True,
        protocols=[ProtocolSpecification.compute_digest(chat_protocol_spec.manifest())],
        description="Canary web chat client", readme="# Canary-Web\n\nGradio front end for Canary.",
        metadata={"categories": ["client"], "is_public": "False"},
    )
    register_agent(req, cfg, RegistrationRequestCredentials(agentverse_api_key=config.AGENTVERSE_API_KEY, agent_seed_phrase=seed))
    return "mailbox registered through the Agentverse API"


class Bridge:
    def __init__(self) -> None:
        self.inbox: list[dict] = []
        self.events: list[dict] = []  # every message this page really sent or received, with its Chat Protocol msg_id
        self.outbox: list[Any] = []
        self.ready = threading.Event()
        self.address = ""
        self.error = ""
        self.target = TARGET if MODE == "mailbox" else config.agent_address("orchestrator")
        self.lock = threading.Lock()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()
        if not self.ready.wait(60):
            raise RuntimeError(self.error or "the chat agent did not start in 60 s")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            seed = _web_seed()
            kwargs: dict[str, Any] = {"name": "Canary-Web", "seed": seed, "port": PORT, "network": "testnet", "loop": loop}
            if MODE == "mailbox":
                if config.AGENTVERSE_API_KEY:
                    print("[web]", _register_mailbox(seed))
                kwargs.update(mailbox=True, agentverse="https://agentverse.ai")
            else:
                kwargs.update(endpoint=[f"http://127.0.0.1:{PORT}/submit"], enable_agent_inspector=False)
            agent = Agent(**kwargs)
            self.address = agent.address

            async def on_text(ctx: Context, sender: str, body: str, msg) -> None:
                cards = chat.extract_cards(msg)
                self.inbox.append({"t": time.time(), "from": sender, "text": body, "cards": cards})
                self.events.append({"t": time.time(), "dir": "in", "peer": sender, "id": str(msg.msg_id),
                                    "what": (body[:90] + (f"  [card: {', '.join(c['kind'] for c in cards)}]" if cards else ""))})

            agent.include(chat.make_chat_protocol(on_text), publish_manifest=False)

            @agent.on_interval(period=0.2)
            async def drain(ctx: Context) -> None:
                while self.outbox:
                    body = self.outbox.pop(0)
                    out = chat.text(body)
                    await ctx.send(self.target, out)
                    self.events.append({"t": time.time(), "dir": "out", "peer": self.target, "id": str(out.msg_id), "what": body[:90]})

            @agent.on_event("startup")
            async def up(ctx: Context) -> None:
                self.ready.set()

            print(f"[web] {MODE} mode; this chat is agent {agent.address}; talking to {self.target}")
            loop.run_until_complete(agent.run_async())
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            print("[web] agent failed:", self.error)
            self.ready.set()

    def send(self, text: str) -> int:
        marker = len(self.inbox)
        self.outbox.append(text)
        return marker

    def stream(self, marker: int, *, first_timeout: float = 90, quiet: float | None = None, final_timeout: float = 330):
        """Yield each reply as it arrives. Stops after a quiet period, or, while an audit runs, at its final message."""
        quiet = quiet if quiet is not None else (4.0 if MODE == "local" else 9.0)  # hosted hops can be slow: wait longer for a follow-up
        start, last, seen, waiting_final = time.time(), time.time(), marker, False
        while True:
            if seen < len(self.inbox):
                entry = self.inbox[seen]
                seen += 1
                last = time.time()
                blob = entry["text"] + " " + " ".join(c["kind"] for c in entry["cards"])
                waiting_final = (waiting_final or bool(RUNNING.search(blob))) and not FINAL.search(blob)
                yield entry
                continue
            idle = time.time() - last
            if seen == marker and time.time() - start > first_timeout:
                return
            if seen > marker and not waiting_final and idle > quiet:
                return
            if waiting_final and time.time() - start > final_timeout:
                return
            time.sleep(0.2)


# --- telemetry: proof that the system is live, not scripted ----------------------------------------------------------


def _agent_book() -> list[tuple[str, str]]:
    if MODE == "local":
        return [(spec.name, config.agent_address(spec.key)) for spec in config.AGENTS.values()]
    book = {"orchestrator": TARGET}
    path = Path(__file__).resolve().parent / "deploy" / "hosted" / "addresses.json"
    if path.is_file():
        book.update(json.loads(path.read_text()))
    return [(config.AGENTS[k].name, a) for k, a in book.items() if k in config.AGENTS]


def _almanac_status(address: str) -> str:
    import httpx

    try:
        r = httpx.get(f"https://agentverse.ai/v1/almanac/agents/{address}", timeout=6)
        if r.status_code == 200:
            return f"{r.json().get('status', 'registered')} ({(r.json().get('type') or '?')})"
        return f"not found ({r.status_code})"
    except Exception as exc:
        return f"unreachable ({type(exc).__name__})"


def telemetry_markdown(bridge: Bridge) -> str:
    rows = ["| Agent | Address | Live status (Almanac) |", "|---|---|---|"]
    for name, addr in _agent_book():
        status = _almanac_status(addr) if MODE == "mailbox" else "local run"
        link = f"[{addr[:14]}…{addr[-6:]}](https://agentverse.ai/agents/details/{addr}/profile)"
        rows.append(f"| {name} | {link} | {status} |")
    lat, sent_at = [], 0.0
    for e in bridge.events:  # latency = a send to the first message that came back after it
        if e["dir"] == "out":
            sent_at = e["t"]
        elif sent_at:
            lat.append(e["t"] - sent_at)
            sent_at = 0.0
    log = ["| Time (UTC) | → / ← | Peer | msg_id | Message |", "|---|---|---|---|---|"]
    for e in bridge.events[-40:][::-1]:
        log.append(f"| {time.strftime('%H:%M:%S', time.gmtime(e['t']))} | {'→ sent' if e['dir'] == 'out' else '← received'} | "
                   f"{e['peer'][:10]}… | {e['id'][:8]} | {e['what'].replace('|', '/').replace(chr(10), ' ')} |")
    stats = (f"**{sum(1 for e in bridge.events if e['dir'] == 'out')} messages sent, "
             f"{sum(1 for e in bridge.events if e['dir'] == 'in')} received** through Agentverse"
             + (f"; first reply after a send took {min(lat):.1f}s fastest, {max(lat):.1f}s slowest" if lat else ""))
    return (
        f"### How to check this is live\nThis page is its own agent (`{bridge.address[:14]}…`) in {MODE} mode. Every line below is a real "
        "Chat Protocol message with its `msg_id`; open any agent's **Logs** tab on Agentverse and you will find the same `msg_id`s. "
        "After an audit, press **View telemetry** on the scorecard for the per-test wire record.\n\n"
        f"{stats}\n\n#### Agents\n" + "\n".join(rows) + "\n\n#### Message log (newest first)\n" + "\n".join(log)
    )


# --- the Gradio page ----------------------------------------------------------------------------------


def build_ui(bridge: Bridge) -> gr.Blocks:
    with gr.Blocks(title="Canary: is this agent safe to pay?") as demo:
        gr.Markdown("# Canary\nAsk whether an Agentverse agent is safe to pay, or have it attacked live and graded A to F. "
                    "Try **Is ShopBot-Solid safe to pay?** or **Audit ShopBot-Leaky**.")
        chatbot = gr.Chatbot(height=520, label="Canary")
        selections = gr.State([])
        with gr.Row():
            buttons = [gr.Button(visible=False, variant="primary" if i == 0 else "secondary") for i in range(MAX_BUTTONS)]
        box = gr.Textbox(placeholder="Message Canary…", show_label=False, autofocus=True)
        with gr.Row():
            for ex in ("Is ShopBot-Solid safe to pay?", "Audit ShopBot-Leaky", "Audit ShopBot-Overcharge", "What does an F grade mean?"):
                gr.Button(ex, size="sm").click(lambda ex=ex: ex, None, box)

        def _button_updates(pairs: list[tuple[str, dict]]):
            pairs = pairs[:MAX_BUTTONS]
            return [gr.update(value=pairs[i][0], visible=True) if i < len(pairs) else gr.update(visible=False) for i in range(MAX_BUTTONS)]

        def talk(text: str, history: list[dict], display: str | None = None):
            history = list(history or [])
            history.append({"role": "user", "content": display or text})
            no_buttons = [gr.update(visible=False)] * MAX_BUTTONS
            yield "", history, [], *no_buttons
            marker, pairs = bridge.send(text), []
            got = False
            for entry in bridge.stream(marker):
                got = True
                md, found = render_message(entry["text"], entry["cards"])
                if found:
                    pairs = found
                if md:
                    history.append({"role": "assistant", "content": md})
                yield "", history, pairs, *_button_updates(pairs)
            if not got:
                history.append({"role": "assistant", "content": "No reply from the Orchestrator in 90 s. Agentverse delivery can be slow; "
                                                                 "try again, or check that the mailbox is connected."})
                yield "", history, [], *no_buttons

        def on_submit(text, history):
            if not (text or "").strip():
                yield "", history, [], *([gr.update(visible=False)] * MAX_BUTTONS)
                return
            yield from talk(text.strip(), history)

        def button_handler(i: int):
            def handler(history, pairs):  # a real generator function: Gradio streams these, not lambdas that return one
                if not pairs or i >= len(pairs):
                    yield "", history, pairs, *([gr.update(visible=False)] * MAX_BUTTONS)
                    return
                label, sel = pairs[i]
                yield from talk(json.dumps(sel), history, display=f"▶ {label}")

            return handler

        outputs = [box, chatbot, selections, *buttons]
        box.submit(on_submit, [box, chatbot], outputs, api_name="respond")
        for i, b in enumerate(buttons):
            b.click(button_handler(i), [chatbot, selections], outputs, api_name=f"button_{i}")

        with gr.Accordion("Telemetry: proof this is live", open=False):
            telemetry = gr.Markdown("Open this section to load live agent status and the message log.")
            refresh = gr.Button("Refresh", size="sm")
            refresh.click(lambda: telemetry_markdown(bridge), None, telemetry, api_name="telemetry")
            gr.Timer(8).tick(lambda: telemetry_markdown(bridge), None, telemetry)
    return demo


def main() -> None:
    bridge = Bridge()
    bridge.start()
    if MODE == "mailbox" and not config.AGENTVERSE_API_KEY:
        print("[web] no AGENTVERSE_API_KEY: if replies never arrive, open the inspector link printed above, "
              "choose Connect > Mailbox once, then restart this page.")
    build_ui(bridge).queue().launch(server_name="127.0.0.1", server_port=int(os.environ.get("PORT", "7860")), inbrowser=False,
                                  show_error=True)


if __name__ == "__main__":
    main()
