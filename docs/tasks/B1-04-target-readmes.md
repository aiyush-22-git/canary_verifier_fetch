# B1-04: Agentverse README for each target

**Goal:** a discoverable Agentverse profile for ShopBot-Leaky, ShopBot-Overcharge and ShopBot-Solid. Judges open these.

**Branch:** `codex/target-readmes`  **Only edit:** `docs/agentverse/target-leaky.md`, `docs/agentverse/target-overcharger.md`,
`docs/agentverse/target-solid.md`, and the `readme_path=` argument in `agents/target.py`

**Each README:** the two hackpack badges first (copy them from `docs/agentverse/orchestrator.md`), a one-line description, what the
agent sells, which tools it has, and the sentence "Demo target for Canary: this agent is deliberately <leaky | too trusting | hardened>.
Run an audit with Canary-Orchestrator." Keywords: shop agent, orders, refunds, discounts, demo target.
Be honest that the weak ones are intentionally weak; never claim they are safe.

**Accept:** each file renders as markdown; `make_agent(key, readme_path=...)` loads it (the Bureau starts).
`uv run pytest -q && LOCAL_ONLY=1 uv run python run.py`

**Paste into Codex:**
```
Read AGENTS.md, docs/CONTEXT.md and docs/STATUS.md first.
Task: docs/tasks/B1-04-target-readmes.md
Only edit: docs/agentverse/target-*.md, agents/target.py (readme_path only)
Never edit core/models.py. If the contract blocks you, stop and explain the problem in the PR description.
Done when: uv run pytest -q && uv run python -m tests.contract_check both pass.
Branch codex/target-readmes, rebase on main, open a PR with the template filled in.
```
