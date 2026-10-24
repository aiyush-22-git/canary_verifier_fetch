# B1-01: core/backboard.py (stateless call and tool-call loop)

**Goal:** the only Backboard client in the repo. Targets use it as their brain; nothing else may call Backboard.

**Branch:** `codex/backboard`  **Only edit:** `core/backboard.py`, `tests/test_backboard.py`

**Spike first (in a scratch folder, not the repo):** pick one tool-capable model with
`GET /api/models?supports_tools=true`, pin its id in `core/backboard.py` as `MODEL`, and make one stateless
tool call work. Record the model id and any quirks in the PR description.

**Build:**
- `async def run(system: str, user: str, tools: list[ToolSpec], *, thread_id: str | None = None) -> str`
  runs the tool-call loop (model asks for a tool -> you execute the Python callable -> feed the result back ->
  repeat, max 5 rounds) and returns the final text.
- `thread_id=None` is a fresh stateless call. A `thread_id` reuses one Backboard thread, which is how the
  *leaky* profile shares memory between customers and *solid* keeps one thread per customer.
- 20 s timeout. On timeout or any Backboard error, raise `BackboardUnavailable`; the caller (target) falls back to
  `core.llm.complete(..., model=config.ASI1_FALLBACK_MODEL)`.
- Key from `core.config.BACKBOARD_API_KEY`. Never log it.

**Contract to honour:** `ToolSpec` is a small dataclass you define in this file: `name`, `description`,
`parameters` (JSON schema dict), `fn` (sync or async callable returning a JSON-serialisable value).

**Accept:** a unit test with a fake HTTP layer and a fake tool shows the loop returns the final text after one
tool round; a second test shows `BackboardUnavailable` on timeout.
`uv run pytest -q && uv run ruff check . && uv run python -m tests.contract_check`

**Paste into Codex:**
```
Read AGENTS.md, docs/CONTEXT.md and docs/STATUS.md first.
Task: docs/tasks/B1-01-backboard.md
Only edit: core/backboard.py, tests/test_backboard.py
Never edit core/models.py. If the contract blocks you, stop and explain the problem in the PR description.
Done when: uv run pytest -q && uv run python -m tests.contract_check both pass.
Branch codex/backboard, rebase on main, open a PR with the template filled in.
```
