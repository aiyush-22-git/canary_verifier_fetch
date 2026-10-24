![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

# Canary: is this agent safe to pay?

**Problem.** Agentverse has millions of agents that take payments, data and instructions from users and from each other, and nobody checks them
first. Generic scanners don't know the Fetch protocols.

**Who it's for.** *Users and agents* about to pay or hand data to an agent get a free, instant trust lookup. *Agent owners* pay for a live audit,
get a grade and exact fixes, and are re-tested on a timer so a regression is caught.

**Outcome.** Inside an ASI:One chat: ask "Is ShopBot-Overcharge safe to pay?", approve an audit plan card, pay through the Payment Protocol, watch
three probes attack the agent live, and get a scorecard: one letter grade, and for every finding a verbatim quote and a fix.

## What makes it different

- **Fetch-native attacks no generic scanner has:** does an agent deliver after a *fake or replayed* payment commit, can a *raw JSON card selection*
  skip its review step, does it *acknowledge every Chat Protocol message*.
- **Evidence nobody can argue with:** each demo target's system prompt carries a unique canary token, so a leak is an exact string match, not a
  model's opinion. Any hard fail is an F. asi1 grades only the soft prompt-injection cases and must quote the transcript.
- **Both sides of payments:** Canary charges for audits as a Payment Protocol seller (idempotent on transaction id, verified server-side) and its payment
  probe attacks other agents' payment handling as a buyer.
- **It keeps running:** verified agents are re-tested on a timer; lookups are answered instantly and free from the registry.
- **Consent first:** only demo agents and agents whose owners proved ownership (one-time code in their Agentverse profile) are ever tested.

## Architecture

```
ASI:One --(Chat + Payment Protocol, Agentverse mailbox)--> Orchestrator  (asi1: intent, plan, cards, seller, timer)
Orchestrator --JudgeRequest--> Judge (hard rules, then asi1 for soft cases)
Orchestrator --ProbeRequest--> Probe-Injection | Probe-Leak | Probe-Payment --Chat/Payment Protocol--> Target-Leaky | -Overcharge | -Solid
Probes --ProbeResult--> Judge --ProbeProgress, Verdict--> Orchestrator --scorecard card--> ASI:One
```

All eight agents run in one uAgents Bureau, so internal hops are local; only the Orchestrator uses the mailbox. See `docs/CONTEXT.md`.

| Agent | Role | Address | Agentverse profile |
|---|---|---|---|
| Canary-Orchestrator | the only agent ASI:One talks to | _fill after registration_ | _link_ |
| Canary-Judge | grades transcripts | _fill_ | _link_ |
| Canary-Probe-Injection / -Leak / -Payment | attack packs | _fill_ | _link_ |
| ShopBot-Leaky / -Overcharge / -Solid | demo targets (Backboard + 4 store tools) | _fill_ | _link_ |

`LOCAL_ONLY=1 uv run python run.py --addresses` prints dev addresses; real ones come from your `SEED_*` values.

## Run it

```bash
uv sync
cp .env.example .env              # fill in your values
LOCAL_ONLY=1 uv run python run.py # dev seeds, no mailbox
uv run python run.py              # real seeds, Orchestrator on the Agentverse mailbox
uv run pytest -q && uv run ruff check . && uv run python -m tests.contract_check
```

Talk to it without ASI:One: `LOCAL_ONLY=1 uv run python -m tests.e2e_client "Is ShopBot-Leaky safe to pay?"` (needs the Bureau running in
another terminal).

## Scope

Hackathon scope, not SaaS: one process, one SQLite file, no login, no web dashboard, test funds only, opt-in allowlist.

## Team and layout

See `AGENTS.md` (ownership, rules, commands) and `docs/STATUS.md` (what's merged, what's next).
