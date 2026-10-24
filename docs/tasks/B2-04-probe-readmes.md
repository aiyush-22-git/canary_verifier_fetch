# B2-04: Agentverse README for each probe

**Goal:** a discoverable Agentverse profile for Canary-Probe-Injection, Canary-Probe-Leak and Canary-Probe-Payment.

**Branch:** `codex/probe-readmes`  **Only edit:** `docs/agentverse/probe-injection.md`, `docs/agentverse/probe-leak.md`,
`docs/agentverse/probe-payment.md`, and the `readme_path=` argument in `agents/probe.py`

**Each README:** the two hackpack badges first (copy from `docs/agentverse/orchestrator.md`), what attack family it runs, which Fetch-specific
failure modes it checks (payment probe: delivery after a fake or replayed commit, wrong amount; injection probe: raw JSON card-selection
spoofing; all: Chat Protocol acknowledgements), that it only runs when Canary-Orchestrator sends an audit for an opted-in agent, and that it
never moves funds. Keywords: agent security, prompt injection, data leak, payment fraud.

**Accept:** files render as markdown; the Bureau starts with them loaded. `uv run pytest -q && LOCAL_ONLY=1 uv run python run.py`

**Paste into Codex:**
```
Read AGENTS.md, docs/CONTEXT.md and docs/STATUS.md first.
Task: docs/tasks/B2-04-probe-readmes.md
Only edit: docs/agentverse/probe-*.md, agents/probe.py (readme_path only)
Never edit core/models.py. If the contract blocks you, stop and explain the problem in the PR description.
Done when: uv run pytest -q && uv run python -m tests.contract_check both pass.
Branch codex/probe-readmes, rebase on main, open a PR with the template filled in.
```
