# STATUS (A1 updates after every merge)

**Phase:** 2 of 5, Codex work can start now. **Gate:** 3 x 3 probe-target matrix (not met: waiting for B1 and B2).

## Merged / ready on `claude/track-a`
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
- Probe/target are stubs: the matrix, hard payment fails and tool-based attacks are untested until B1/B2 land.
- Cards are schema-validated locally but not yet pasted into the ASI:One Card Playground (needs a human, 5 minutes). `detail` cards omit `sub_options`; if the Playground insists on it, add one to `core/cards.py`.
- Payments: FET and Stripe code paths written from the fetch-skills reference; untested against a real ledger/Stripe (no funds/keys in CI). Run the Phase 4 payment spike on a real chat before the freeze.
- An all-skipped audit (target silent) is reported with a "couldn't test" notice and is not cached.
- Architecture image for the README is not made yet (ASCII diagram only).

## Human-only steps (need accounts, keys, or a browser)
1. Fill `.env` (`ASI1_API_KEY`, `BACKBOARD_API_KEY`, `AGENTVERSE_API_KEY`, one `SEED_*` per agent, `PAYMENT_RAIL`) on **A1's machine only**.
2. `uv run python run.py --addresses`, run the Bureau, register the 8 agents on Agentverse, copy the addresses into the README table.
3. Find the Orchestrator from ASI:One, save the shared chat link, verify the 10-second reply (Phase 1 gate).
4. Phase 4 payment spike: pick the rail ASI:One completes end to end (Stripe test mode or FET stable-testnet) and set `PAYMENT_RAIL`.
5. Paste each card payload into the Card Playground.

## Env keys
`ASI1_API_KEY`, `BACKBOARD_API_KEY`, `AGENTVERSE_API_KEY`, `LOCAL_ONLY`, `SEED_*`, `PAYMENT_RAIL`. See `.env.example`.

## Agent addresses
LOCAL_ONLY dev addresses: `LOCAL_ONLY=1 uv run python run.py --addresses`. Real addresses: README, after registration.
