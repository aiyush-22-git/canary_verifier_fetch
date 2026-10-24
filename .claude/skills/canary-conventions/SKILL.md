---
name: canary-conventions
description: Canary's repo rules. Load first, in every session and for every subagent, before touching code in this repo. Covers the five clean-code rules, the message contracts in core/models.py, file ownership, and how to add an attack case, a target profile or a card.
---

# Canary conventions

Read `docs/CONTEXT.md` (architecture, event strings, shared vocabulary) and `docs/STATUS.md` (what is merged, what is next) first.

## The five rules

1. Nothing outside `core/llm.py` imports the OpenAI SDK; nothing outside `core/backboard.py` calls Backboard.
2. Every agent sends messages through `core/chat.py`; no agent builds a `ChatMessage` by hand.
3. Adding a test means adding YAML lines in `packs/`, never a new Python branch.
4. Every inter-agent message is a model in `core/models.py`; changing one goes through the lead (PR labelled `contract`, announced in `docs/STATUS.md`).
5. Handlers only route: they call `core/` functions and send the result.

## Ownership (write only inside your area)

| Area | Owner |
|---|---|
| `core/` except `backboard.py`, `cards.py`; `agents/orchestrator.py`; `run.py`; `README.md`; `AGENTS.md`; `docs/` | lead (A1) |
| `agents/judge.py`, `core/cards.py`, `tests/` | A2 |
| `agents/target.py`, `targets/`, `core/backboard.py` | B1 (Codex, PRs only) |
| `agents/probe.py`, `packs/` | B2 (Codex, PRs only) |

## Contracts you will use

`AttackCase` (a pack row), `Pack`, `TargetProfile`, `ProbeRequest`, `Exchange`, `ProbeResult`, `JudgeRequest`, `ProbeProgress`, `Finding`,
`ProbeSummary`, `Verdict`, `AuditPlan`, `RegistryEntry`, all in `core/models.py`. Vocabulary tuples (`CATEGORIES`, `ACTIONS`, `RULE_KINDS`,
`SEVERITIES`, `GRADES`) are validated by `core/packs.py` and `tests/contract_check.py`.

## How to add things

- **An attack case:** add a row to `packs/<category>.yaml` with `id, category, title, action, messages|raw_json, rule_kind, rule_arg|rubric,
  severity, hard, fix, expected`. Hard rules are exact matches only. Run `uv run python -m tests.contract_check`.
- **A target profile:** add a `TargetProfile` to `targets/profiles.yaml`; its `canary` must equal the one in `core/config.py::DEMO_TARGETS` and appear
  in its `system_prompt`. Behaviour is flags, not code branches.
- **A card:** add a pure function to `core/cards.py` returning `CardSpec(kind, payload, one_line_summary)`; one primary action; selections carry
  identifiers only (the Orchestrator re-checks them against the registry). Add it to `SPECS` in `tests/test_cards.py` so it is schema-validated.
- **An agent:** `make_agent(key)` from `core/agentkit.py`; chat through `chat.make_chat_protocol`; never block in a handler without a timeout.

## Behaviours worth remembering

- A selection never proves anything. Payment and audit steps are checked against `registry.pending` and `registry.payments`.
- A replayed transaction id is rejected (`payments.tx_id` is unique).
- Never fake a completed payment in the demo. The only fake is `payments._verify_fet` monkeypatched in tests.
- asi1 failing must degrade, not crash: rules-only verdicts are marked partial; intent falls back to keywords.
- Commands: `uv sync`, `uv run pytest -q`, `uv run ruff check .`, `uv run python -m tests.contract_check`, `LOCAL_ONLY=1 uv run python run.py`.
