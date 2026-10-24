# STATUS (A1 updates after every merge)

**All work lives on `main`; Codex: branch from `main`, open PRs into `main`, rebase before pushing.**

**Phase:** 2 of 5, Codex work can start now. **Gate:** 3 x 3 probe-target matrix (not met: waiting for B1 and B2).

## Merged on `main`
- Contracts (`core/models.py`), config, `llm` (asi1), `chat`, `registry`, `agentkit`, `payments`, `intent`, `planner`, `packs` loader, dev `stubs`
- Orchestrator (intent, plan, paid audit, replay rejection, forged-selection refusal, owner verification, re-verify timer), Judge, six cards + notice card
- `run.py` Bureau with stub probes and targets, CI, PR template, `tests/contract_check.py`, simulated ASI:One client (`tests/e2e_client.py`)
- All tests green on the stubs: judge fixtures grade F, F, A (twice in a row); e2e paid audit of the stub Leaky target grades F in about 30 s

## In flight
- B1 and B2: nothing yet. Start with task 1 each (below).

## Next
| # | B1 (targets) | B2 (probes) |
|---|---|---|
| 1 | `docs/tasks/B1-01-backboard.md` | `docs/tasks/B2-01-packs.md` |
| 2 | `docs/tasks/B1-02-tools-store.md` | `docs/tasks/B2-02-probe-agent.md` |
| 3 | `docs/tasks/B1-03-target-agent.md` | `docs/tasks/B2-03-payment-probe.md` |
| 4 | `docs/tasks/B1-04-target-readmes.md` | `docs/tasks/B2-04-probe-readmes.md` |

Entry points `run.py` expects: `agents.probe.build_probe(kind)` and `agents.target.build_target(key)`. Until they exist the stubs in `core/stubs.py` run.

## Broken / known gaps
- Ports moved to 8100-8107 (8000 collides with common dev servers). `tests/test_e2e.py` needs 8100 free: stop any running `run.py` before `pytest`.
- LOCAL_ONLY agents still self-register on the testnet Almanac (uAgents default) with public dev seeds; harmless, local delivery is used first.
- Probe/target are stubs: the matrix, hard payment fails and tool-based attacks are untested until B1/B2 land.
- Cards are schema-validated locally but not yet pasted into the ASI:One Card Playground (needs a human, 5 minutes). `detail` cards omit `sub_options`; if the Playground insists on it, add one to `core/cards.py`.
- Payments: default rail is **Dodo Payments** (hosted checkout link + server-side verification, tested against a fake Dodo API only). FET and Stripe are alternatives, also untested live. Needs `DODO_PAYMENTS_API_KEY` + `DODO_PRODUCT_ID`, then one real test-mode payment from a fresh ASI:One chat (docs/LIVE.md section 7).
- An all-skipped audit (target silent) is reported with a "couldn't test" notice and is not cached.
- Architecture image for the README is not made yet (ASCII diagram only).

## Human-only steps (need accounts, keys, or a browser)
Full runbook: `docs/LIVE.md`. Helper: `uv run python live.py init-env | check | register | addresses`; real-asi1 tests: `uv run pytest -m live`.
0. CI: the push token had no `workflow` scope, so the workflow lives at `docs/ci.yml.template`. Copy it to `.github/workflows/ci.yml` with a token that has the scope (or via the GitHub web UI), then protect `main` with that check.
1. Fill `.env` (`ASI1_API_KEY`, `BACKBOARD_API_KEY`, `AGENTVERSE_API_KEY`, one `SEED_*` per agent, `PAYMENT_RAIL`) on **A1's machine only**.
2. `uv run python run.py --addresses`, run the Bureau, register the 8 agents on Agentverse, copy the addresses into the README table.
3. Find the Orchestrator from ASI:One, save the shared chat link, verify the 10-second reply (Phase 1 gate).
4. Create the Dodo test-mode product + API key, set `DODO_PAYMENTS_API_KEY` / `DODO_PRODUCT_ID`, make one real test payment.
5. Paste each card payload into the Card Playground.

## Env keys
`ASI1_API_KEY`, `BACKBOARD_API_KEY`, `AGENTVERSE_API_KEY`, `LOCAL_ONLY`, `SEED_*`, `DODO_PAYMENTS_API_KEY`, `DODO_PRODUCT_ID`, `PAYMENT_RAIL`. See `.env.example`.

## Agent addresses
LOCAL_ONLY dev addresses: `LOCAL_ONLY=1 uv run python run.py --addresses`. Real addresses: README, after registration.
