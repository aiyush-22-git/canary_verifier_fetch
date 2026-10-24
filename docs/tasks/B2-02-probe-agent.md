# B2-02: agents/probe.py (ProbeAgent)

**Goal:** `ProbeAgent(kind)`: one class, three instances (`injection`, `leak`, `payment`). It receives a `ProbeRequest`, attacks the target
case by case and sends one `ProbeResult` to the Judge.

**Branch:** `codex/probe-agent`  **Only edit:** `agents/probe.py`, `tests/test_probe.py`
(`core/stubs.py::build_stub_probe` is a working minimal example; do not edit it)

**Build:** `build_probe(kind: str) -> Agent` via `make_agent(config.probe_key(kind))`.
- `@agent.on_message(ProbeRequest)`: for each `AttackCase`, run its `action` (table in docs/CONTEXT.md), record an `Exchange`, then
  `ctx.send(req.judge_address, ProbeResult(...))`. Cases of one request run in order.
- One conversation per target at a time: an `asyncio.Lock` per `target_address`.
- Per-case timeout `req.timeout_s` (15 s): on timeout set `timed_out=True` and keep what arrived. A silent target is data, not an exception.
- Chat via `core.chat` only: `make_chat_protocol(on_text, on_ack)`. Fill a per-target `asyncio.Queue` in `on_text`, record ACKs in `on_ack`
  keyed by `acknowledged_msg_id`. Log cards the target sends with `chat.extract_cards` as `Card kind=.. actions=..` events.
- Never block in a handler without a timeout; message handling is concurrent (see docs/CONTEXT.md).
- Fill `Exchange.sent`, `received`, `events`, `sent_chat`, `acked`, `latency_ms` exactly as docs/CONTEXT.md defines. Do not judge anything.
- A probe sends no hard-coded attack text: everything comes from `case.messages` / `case.raw_json`.
- Payment actions come in B2-03; for now an unknown `action` produces an `Exchange(error="unsupported action")`.

**Accept:** `LOCAL_ONLY=1 uv run python run.py` then the 3 x 3 matrix: `tests/test_probe.py` runs all three probes against all three
targets (or the stub targets until B1 lands) through the Bureau and asserts one ProbeResult per probe with one Exchange per case.
`uv run pytest -q && uv run ruff check . && uv run python -m tests.contract_check`

**Paste into Codex:**
```
Read AGENTS.md, docs/CONTEXT.md and docs/STATUS.md first.
Task: docs/tasks/B2-02-probe-agent.md
Only edit: agents/probe.py, tests/test_probe.py
Never edit core/models.py. If the contract blocks you, stop and explain the problem in the PR description.
Done when: uv run pytest -q && uv run python -m tests.contract_check both pass.
Branch codex/probe-agent, rebase on main, open a PR with the template filled in.
```
