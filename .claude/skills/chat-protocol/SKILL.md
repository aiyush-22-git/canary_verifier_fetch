---
name: chat-protocol
description: Protocol knowledge for building Fetch.ai uAgents that speak the official Chat Protocol. Use when wrapping existing agent code, adding chat capability to a function, or creating a new chat-capable agent from a prompt. Covers ChatMessage/ChatAcknowledgement handling, TextContent structure, session lifecycle, and Agentverse manifest registration.
---

# uAgent Chat Protocol (Fetch.ai)

## Purpose
Give the coding agent enough protocol knowledge to wrap **any** function, existing uAgent, or new flow with a compliant Chat Protocol layer.

## When to Use
- Wrapping an existing function/agent so it speaks Chat Protocol
- Adding chat handlers to an existing uAgent
- Creating a new chat-capable uAgent from a prompt
- Any task referencing `ChatMessage`, `ChatAcknowledgement`, `chat_protocol_spec`, or Agentverse chat

## When NOT to Use
- Mock/pseudo implementations
- Non-uAgents frameworks
- Tasks where protocol compliance is not required

---

## Package Setup

Before running install commands, creating `pyproject.toml`, or generating `.env.example` / `.gitignore`, check which **package setup skill** is installed and defer to it:

- `uv-package` for `uv` projects
- `poetry-package` for Poetry projects
- `python-venv-package` for `venv` + `pip` projects
- `no-package` for code-only mode (no install commands)

This protocol skill only defines chat protocol code. It does not own dependency installation, lockfile management, or virtualenv creation.

### Package Skill Precedence (mandatory)

Use this decision order before emitting any setup/install/run command:

1. Detect which package skills are actually available in the current session from this set only: `uv-package`, `no-package`, `poetry-package`, `python-venv-package`.
2. If the user explicitly asks for one of those managers and the matching skill is available, use it.
3. Otherwise, if repository signals are present, select the matching available skill:
   - `uv.lock` or `[tool.uv]` -> `uv-package`
   - `poetry.lock` or `[tool.poetry]` -> `poetry-package`
   - `requirements.txt` / existing `.venv` conventions -> `python-venv-package`
4. If no manager is detectable, choose one available skill and stay consistent for the full response. Prefer this fallback order: `uv-package` -> `poetry-package` -> `python-venv-package` -> `no-package`.
5. Never emit commands for a package skill that is not available.

When a package skill is active, all dependency/setup/run instructions must come from that skill only:

- `uv-package` -> `uv add ...`, `uv run python agent.py`
- `poetry-package` -> `poetry add ...`, `poetry run python agent.py`
- `python-venv-package` -> `pip install ...` (and project's venv flow)
- `no-package` -> no install commands; only document dependencies

Base runtime dependencies expected by the snippets below: `uagents`, `uagents-core`, `python-dotenv`. Add them through whichever package skill is active (`uv add ...`, `poetry add ...`, `pip install ...`, or document-only for `no-package`).

---

## Required Imports

```python
import os
from datetime import datetime, timezone
from uuid import uuid4
from uagents import Agent, Protocol, Context
from uagents_core.contrib.protocols.chat import (
    ChatMessage, ChatAcknowledgement, TextContent,
    StartSessionContent, EndSessionContent, chat_protocol_spec,
)

# If the agent reads any env var (seed, API keys, etc.), also:
from dotenv import load_dotenv
load_dotenv()
```

---

## Directional Logging Contract

Every agent generated from this skill must emit structured `[inbound]` and `[outbound]` logs so the user can trace the chat lifecycle directly from the CLI. This makes production debugging tractable and keeps message flow auditable end-to-end.

### Required tags

- `[inbound]` — log the moment a message is received, **before** any processing. Always include the message type and the sender address.
- `[outbound]` — log immediately **before** every `ctx.send(...)`. Always include the message type and the recipient address.
- `[state]` — log notable storage transitions (session created, session ended, stored resources).
- `[error]` — use `ctx.logger.error(...)` with context; never swallow failures silently.

### Redaction rules

- Never log raw secrets (API keys, seeds, signed payloads).
- Never log full `ResourceContent` byte payloads — log size and mime type only.
- Truncate user text in logs (e.g. first 120 chars) when messages may be long.

### Helpers (copy into `protocols/chat_proto.py`)

```python
def log_inbound(ctx: Context, msg_type: str, sender: str, details: str | None = None) -> None:
    suffix = f" {details}" if details else ""
    ctx.logger.info(f"[inbound] {msg_type} from {sender}{suffix}")


def log_outbound(ctx: Context, msg_type: str, recipient: str, details: str | None = None) -> None:
    suffix = f" {details}" if details else ""
    ctx.logger.info(f"[outbound] {msg_type} -> {recipient}{suffix}")


def log_state(ctx: Context, event: str, details: str | None = None) -> None:
    suffix = f" {details}" if details else ""
    ctx.logger.info(f"[state] {event}{suffix}")
```

Every `ctx.send(...)` call in generated code must be preceded by a `log_outbound(...)`. Every `@proto.on_message(...)` handler must start with a `log_inbound(...)`.


Required testnet funding helper — import it whenever the agent runs on testnet (the development default):
```python
from uagents.setup import fund_agent_if_low
```

This is what suppresses the noisy `WARNING: [uagents.registration] I do not have enough funds to register on Almanac contract` log line. Without it, every cold start emits the warning until someone manually funds the wallet.

> See **Environment Variables** below for the full pattern + `.env.example` generation.

---

## The 8 Mandatory Blocks

Any agent using Chat Protocol must contain these, in this order:

### 1. Agent init (mailbox + Agentverse + testnet)

Development default is `network="testnet"` — this is what makes the agent register against the testnet Almanac contract instead of mainnet. Switch to `network="mainnet"` only after the user explicitly opts into a production deploy.

```python
agent = Agent(
    name=..., port=..., seed=...,
    mailbox=True,
    agentverse="https://agentverse.ai",
    network="testnet",
)

# REQUIRED on testnet: top up the agent wallet so Almanac registration succeeds.
# Without this call uagents logs:
#   WARNING: [uagents.registration] I do not have enough funds to register on Almanac contract
#   WARNING: [uagents.registration] To enable contract registration, send funds to wallet address: fetch1...
# fund_agent_if_low is a no-op once the wallet already has enough testnet FET, so it's safe on every restart.
fund_agent_if_low(str(agent.wallet.address()))
```

Hard rule: every generated agent that uses `network="testnet"` must call `fund_agent_if_low(str(agent.wallet.address()))` immediately after `Agent(...)`. Skip the call only when targeting mainnet, where the user is expected to fund the wallet themselves.

### 2. Protocol init
```python
chat_proto = Protocol(spec=chat_protocol_spec)
```

### 3. `create_text_chat` helper
Wraps plain text into a valid `ChatMessage`. Use timezone-aware UTC (`datetime.now(timezone.utc)`) — `datetime.utcnow()` is deprecated in Python 3.12+ and emits naive datetimes the protocol normalizers can mis-handle.
```python
def create_text_chat(text: str) -> ChatMessage:
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=[TextContent(type="text", text=text)],
    )
```

### 4. `ChatMessage` handler (core logic)
Must log sender → **ACK first** → iterate `msg.content` → branch per content type. The handler must start with `log_inbound` and every `ctx.send(...)` must be preceded by `log_outbound`.
```python
@chat_proto.on_message(ChatMessage)
async def handle_chat_message(ctx: Context, sender: str, msg: ChatMessage):
    log_inbound(ctx, "ChatMessage", sender, f"msg_id={msg.msg_id}")

    ack = ChatAcknowledgement(
        timestamp=datetime.now(timezone.utc),
        acknowledged_msg_id=msg.msg_id,
    )
    log_outbound(ctx, "ChatAcknowledgement", sender, f"acknowledged_msg_id={msg.msg_id}")
    await ctx.send(sender, ack)

    for item in msg.content:
        if isinstance(item, StartSessionContent):
            log_state(ctx, "session_started", f"sender={sender}")
        elif isinstance(item, TextContent):
            preview = (item.text or "")[:120]
            ctx.logger.info(f"[inbound] text from {sender}: {preview!r}")
            response = your_logic(item.text)
            reply = create_text_chat(response)
            log_outbound(ctx, "ChatMessage", sender, f"reply_msg_id={reply.msg_id}")
            await ctx.send(sender, reply)
        elif isinstance(item, EndSessionContent):
            log_state(ctx, "session_ended", f"sender={sender}")
        else:
            ctx.logger.warning(f"[inbound] unknown content from {sender}: {type(item).__name__}")
```

### 5. `ChatAcknowledgement` handler
```python
@chat_proto.on_message(ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    log_inbound(ctx, "ChatAcknowledgement", sender, f"acknowledged_msg_id={msg.acknowledged_msg_id}")
```

### 6. Protocol registration
```python
agent.include(chat_proto, publish_manifest=True)
```
`publish_manifest=True` is what makes the agent discoverable on Agentverse.

### 7. Entrypoint
```python
if __name__ == "__main__":
    agent.run()
```

---

## Wrapping Patterns

**Wrap an existing function** → call it inside the `TextContent` branch, wrap return value via `create_text_chat`.

**Wrap an existing uAgent** → add `chat_proto`, add both handlers, call `agent.include(chat_proto, publish_manifest=True)`. Leave existing protocols intact.

**Create from scratch** → emit all 8 blocks in order.

---

## Default LLM (ASI:One)

ASI:One is the fallback LLM for chat handlers — used **only** when an LLM is needed, the user did not pick one, and the project has no existing LLM wiring. ASI:One is OpenAI Chat Completions API-compatible, so the standard `openai` Python SDK works unchanged — only `api_key` and `base_url` move.

### Decision: which LLM to use

Walk the rules in order; pick the first match and stop.

1. **Does the task actually need an LLM?** If the chat handler is a plain function, a deterministic API call, or anything non-LLM, do NOT add any LLM client. Skip this section.
2. **Did the user explicitly name a provider or model?** (e.g. `openai`, `gpt-4o`, `gpt-5`, `claude-3.5-sonnet`, `anthropic`, `gemini`, `groq`, `ollama`, `together`, `mistral`, etc.) — use exactly that. Never override an explicit choice with ASI:One. Treat `asi1`, `asi:one`, `asi-one`, `asione`, "ASI", or "ASI One" as an explicit ASI:One choice.
3. **Does the project already have an LLM wired up?** (see *Existing LLM detection* below) — reuse the project's existing client/wrapper. Never replace it.
4. **Otherwise** — use ASI:One as the default.

In one line: ASI:One is the fallback only when the chat handler needs an LLM **and** the user did not name one **and** the project has no existing LLM wiring.

### Existing LLM detection

Before adding ASI:One, scan the project for any of these signals. If **any** are present, leave the project's LLM untouched and skip the rest of this section.

- Provider env keys declared in `.env`, `.env.example`, or referenced via `os.getenv(...)`: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` / `GEMINI_API_KEY`, `MISTRAL_API_KEY`, `GROQ_API_KEY`, `TOGETHER_API_KEY`, `COHERE_API_KEY`, `OLLAMA_*`, `AZURE_OPENAI_*`, or any other provider key.
- Existing imports of `openai`, `anthropic`, `google.generativeai`, `mistralai`, `groq`, `together`, `cohere`, `langchain_*`, `llama_index`, `litellm`, or any other LLM client.
- An existing wrapper module (e.g. `llm.py`, `clients/llm.py`, `services/ai.py`) — reuse it.
- `ASI_ONE_API_KEY` already declared or referenced — reuse the existing wiring; never add it twice.

### Client setup (Python, OpenAI SDK)

```python
import os
from openai import OpenAI

asi_client = OpenAI(
    api_key=os.getenv("ASI_ONE_API_KEY"),
    base_url="https://api.asi1.ai/v1",
)

response = asi_client.chat.completions.create(
    model="asi1",
    messages=[
        {"role": "system", "content": "Be precise and concise."},
        {"role": "user", "content": user_text},
    ],
    temperature=0.2,
    max_tokens=1000,
)

reply_text = response.choices[0].message.content
```

Drop `reply_text` straight into `create_text_chat(reply_text)` from the `TextContent` branch.

### Required env var

When ASI:One is the default, add **only** `ASI_ONE_API_KEY` to `.env.example`. Do not add `OPENAI_API_KEY` (different provider) and never invent placeholder keys.

```
# ASI:One LLM (OpenAI-compatible; default when no other provider is configured)
ASI_ONE_API_KEY=
```

Validate it at startup the same way `AGENT_SEED` is validated — fail fast if missing rather than silently degrading at request time.

### Dependency

Add the official `openai` Python SDK only when the generated code actually instantiates the client. Route the install through the active package skill (`uv add openai`, `poetry add openai`, `pip install openai`, or document-only for `no-package`). Do not pin a version unless the project already pins others.

### Optional knobs (use only when the use case needs them)

ASI:One accepts every standard OpenAI parameter (`temperature`, `top_p`, `max_tokens`, `presence_penalty`, `frequency_penalty`, `stream`) plus two extras exposed through the OpenAI SDK's escape hatches:

- **Web search** — pass `extra_body={"web_search": True}` to let the model browse, `False` to force it off. Omit otherwise.
- **Agentic session persistence** — pass `extra_headers={"x-session-id": session_id}` to keep multi-turn agentic state across calls. Use a stable string (`uuid.uuid4()` is fine) per chat session, not per message.

```python
import uuid
session_id = str(uuid.uuid4())

response = asi_client.chat.completions.create(
    model="asi1",
    messages=[{"role": "user", "content": user_text}],
    extra_headers={"x-session-id": session_id},
    extra_body={"web_search": False},
    stream=True,
)
```

### Streaming

ASI:One occasionally emits chunks with no `choices` or empty `delta.content`. Guard every chunk:

```python
for chunk in response:
    if (
        getattr(chunk, "choices", None)
        and chunk.choices
        and getattr(chunk.choices[0], "delta", None)
        and chunk.choices[0].delta.content
    ):
        print(chunk.choices[0].delta.content, end="")
```

### Response shape

- `response.choices[0].message.content` — main reply text. Always read this for plain chat.
- `response.usage` — token usage (`prompt_tokens`, `completion_tokens`, `total_tokens`).
- `response.model` — model name echoed back.
- ASI:One-specific fields (read defensively with `getattr`, may be absent): `executable_data` (Agentverse agent calls / tool manifests), `intermediate_steps` (multi-step reasoning trace), `thought` (model reasoning).

### Hard rules for ASI:One

- Never hardcode the API key — always `os.getenv("ASI_ONE_API_KEY")`.
- Never log the API key, full streamed text, or `intermediate_steps` / `thought` payloads — they may carry tool arguments or sensitive context. Log lengths only, consistent with the Directional Logging Contract above.
- `base_url` is always exactly `https://api.asi1.ai/v1`. Do not call `https://api.openai.com` while routing to ASI:One.
- Do not silently fall back to another provider if ASI:One is unreachable — surface the error and let the user decide. Silent fallback hides outages and bills the wrong account.

---

## Hard Rules

- ACK every `ChatMessage` **before** any processing
- Default `Agent(...)` to `network="testnet"` for development; switch to `"mainnet"` only when the user explicitly opts into a production deploy
- On `network="testnet"`, always call `fund_agent_if_low(str(agent.wallet.address()))` immediately after `Agent(...)` — this prevents the `[uagents.registration] I do not have enough funds to register on Almanac contract` warning loop
- Always use timezone-aware UTC: `datetime.now(timezone.utc)`. Never `datetime.utcnow()` (deprecated in Python 3.12+) and never naive `datetime.now()`.
- Fresh `uuid4()` per message
- `msg.content` is a **list** — always iterate
- All response text goes through `TextContent` (via `create_text_chat`)
- `publish_manifest=True` is mandatory
- Unknown content types → log warning, continue (never crash)
- Every handler starts with `log_inbound(...)`; every `ctx.send(...)` is preceded by `log_outbound(...)`
- State transitions (session start/end, stored resources) are logged with `log_state(...)`

## Forbidden

- Sending raw strings via `ctx.send`
- Skipping the acknowledgement
- Reusing `msg_id` values
- Indexing `msg.content[0]` instead of iterating
- Omitting `publish_manifest=True`
- Omitting the `ChatAcknowledgement` handler
- Mock/fake protocols — always use `chat_protocol_spec`
- Calling `ctx.send(...)` without a preceding `log_outbound(...)` line
- Logging raw secrets, seeds, full byte payloads, or unbounded user text (truncate at ~120 chars)

---

## Message Shapes (reference)

**ChatMessage**
```
timestamp: datetime (UTC)
msg_id: UUID4
content: list[TextContent | StartSessionContent | EndSessionContent]
```

**ChatAcknowledgement**
```
timestamp: datetime (UTC)
acknowledged_msg_id: UUID4
```

## Flow

1. Peer sends `ChatMessage` → agent
2. Agent sends `ChatAcknowledgement` back immediately
3. Agent processes each item in `msg.content`
4. Agent sends response `ChatMessage`
5. Peer sends `ChatAcknowledgement` back

---

## Environment Variables

Whenever the agent reads **any** value from `os.getenv(...)` (seed, API keys, ports, URLs, etc.), load a `.env` file at the top of `agent.py` so the user can just fill in `.env` and run.

### Import pattern (put at the top of `agent.py`, right after `os`)

```python
import os
from dotenv import load_dotenv

load_dotenv()  # reads .env into os.environ
```

### Dependency
- Add `python-dotenv` to the dependency file **only if** the agent reads env vars.
- If the agent has no env vars at all, skip `python-dotenv` entirely.

### Generate `.env.example`
Whenever `load_dotenv()` is used, also create a `.env.example` in the project root listing every env var the agent reads, with placeholder values and short comments:

```
# Agent identity (required)
AGENT_SEED=your_unique_seed_phrase_here

# API keys for handler logic (only include the ones the agent actually uses).
# Pick exactly one LLM section per the "Default LLM (ASI:One)" decision tree:
#   - Project has no LLM and the handler needs one  -> uncomment ASI_ONE_API_KEY
#   - Project already uses OpenAI / Anthropic / etc. -> keep that key, do NOT add ASI_ONE_API_KEY
#   - Handler does not call any LLM                  -> remove all of these lines
# ASI_ONE_API_KEY=
# OPENAI_API_KEY=sk-...
# ANTHROPIC_API_KEY=sk-ant-...

# Optional overrides
# AGENT_PORT=8000
```

Rules:
- `.env.example` lists **only** the vars the agent actually reads — don't pad it.
- Never create a real `.env` file; only `.env.example`.
- If `.gitignore` exists, ensure `.env` is in it (append the line if missing). If `.gitignore` doesn't exist, skip it — don't create one.

### README install step update
When `.env.example` is generated, the README install section must match the active package skill:

```bash
cp .env.example .env          # then fill in your values
# then run with your active package skill:
# uv-package -> uv run python agent.py
# poetry-package -> poetry run python agent.py
# python-venv-package -> python agent.py (inside venv)
# no-package -> document run command only, no install commands
```

---

## File Output Conventions (lower priority, but do this)

After writing the agent code, also handle these — keep it minimal, no extra scaffolding.

### Agent file name
- Default to **`agent.py`**.
- If `agent.py` already exists and is a different agent, pick a non-colliding name (`chat_agent.py`, `<purpose>_agent.py`). Never overwrite an unrelated file.

### Dependencies
Declare dependencies via the active package skill only:

- Always include `uagents` and `uagents-core`.
- Include `python-dotenv` only if the agent uses `load_dotenv()`.
- Add other libraries only if used by handler logic.
- Do not pin versions unless the project already pins others.

Do not create or mutate dependency manifests manually in this skill; defer manifest/lockfile behavior to the active package skill.

### README
Create or append to `README.md` — keep it to three sections only:

```markdown
# <Agent Name>

<One-line description of what the agent does.>

## Installation

```bash
cp .env.example .env              # then fill in your values (only if .env.example exists)
# Run command must follow active package skill:
# uv-package -> uv run python agent.py
# poetry-package -> poetry run python agent.py
# python-venv-package -> python agent.py (inside venv)
# no-package -> document-only mode; no install commands
```
```

If the agent doesn't use env vars, drop the `cp .env.example .env` line.

No badges, no architecture diagrams, no contribution guide, no license section unless asked.

### Rules
- Resolve package manager via the **Package Skill Precedence** block before any setup/run instructions.
- If a package skill is active, all setup/install/run commands must match that skill only.
- Never output mixed package-manager commands in one solution (for example `uv add ...` plus `pip install ...`).
- Never overwrite an existing `README.md` — append a section for this agent instead.
- Never duplicate a dependency that's already declared.
- Never introduce new config files (Docker, CI, lint configs) unless the user asked.

---

## Log Verification Checklist

Before handing the agent to the user, run it locally and confirm CLI output contains:

- `[inbound] ChatMessage from <sender>` when a message arrives — before any processing.
- `[outbound] ChatAcknowledgement -> <sender>` immediately after, before the actual send.
- `[outbound] ChatMessage -> <sender>` preceding every reply.
- `[state] session_started` / `[state] session_ended` for session boundary markers.
- `[inbound] ChatAcknowledgement from <peer>` when the peer ACKs.
- No raw secrets, seeds, or full byte payloads anywhere in the logs.
- User text previews are truncated (≤120 chars) — no runaway multi-KB log lines.

If any of these are missing, treat it as a skill-compliance bug and fix before shipping.

---

## Summary

Use `chat_protocol_spec` + `Protocol` + two handlers + the helper. ACK first, iterate content, wrap text in `TextContent`, publish the manifest. Anything else — existing functions, LLMs, tool calls, custom flows — plugs into the `TextContent` branch. When the agent needs an LLM and the user has not picked one, default to ASI:One via the OpenAI SDK pointed at `https://api.asi1.ai/v1` with `model="asi1"`; never override an LLM the project already uses. If the agent reads any env var, call `load_dotenv()` at the top and ship a `.env.example`. Then declare `uagents` + `uagents-core` (plus `python-dotenv` if used, and `openai` if the ASI:One default is wired in) through the active package skill, and write a minimal README with title + description + install steps that match that active package skill. Every receive/send step must be traceable from CLI logs via the `[inbound]`/`[outbound]`/`[state]` tags.