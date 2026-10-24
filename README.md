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
- **Both sides of payments:** Canary charges for audits (Dodo Payments hosted checkout, or Stripe/FET through the Payment Protocol), verifies every payment server-side and rejects replays and its payment
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

All eight agents are deployed as hosted agents on Agentverse (they can also run in one local uAgents Bureau via `run.py`). See `docs/CONTEXT.md`.

| Agent | Role | Address | Agentverse profile |
|---|---|---|---|
| Canary-Orchestrator | the only agent ASI:One talks to | `agent1qw4vnrljtcgkfnxwj3e4tlvhvxrp7jjxr4thulgkrs7kesmv6gd2way6kd6` | [profile](https://agentverse.ai/agents/details/agent1qw4vnrljtcgkfnxwj3e4tlvhvxrp7jjxr4thulgkrs7kesmv6gd2way6kd6/profile) |
| Canary-Judge | grades transcripts (hard rules + asi1 soft grading) | `agent1qv7p4yrd4dzyr67wygheezg0el3mpm24asyw0u5xrqz39edrtlchqc33rys` | [profile](https://agentverse.ai/agents/details/agent1qv7p4yrd4dzyr67wygheezg0el3mpm24asyw0u5xrqz39edrtlchqc33rys/profile) |
| Canary-Probe-Injection | prompt / tool-output injection pack | `agent1q0fwekwwlstkhtaz5330frx4shutj96dppmj8wkh4x6f4za0c938ugdrvwh` | [profile](https://agentverse.ai/agents/details/agent1q0fwekwwlstkhtaz5330frx4shutj96dppmj8wkh4x6f4za0c938ugdrvwh/profile) |
| Canary-Probe-Leak | canary-token and data-leak pack | `agent1qdpvxn5ge2vm8ayexnjmnnwjs62nrudyrklkv86hxqwrrr66758fk0scj2n` | [profile](https://agentverse.ai/agents/details/agent1qdpvxn5ge2vm8ayexnjmnnwjs62nrudyrklkv86hxqwrrr66758fk0scj2n/profile) |
| Canary-Probe-Payment | fake / replayed / short payment pack | `agent1q2yevx2vgmj7csvul5s7e7yucaf2l6gxzw9708vvc2ftjltye0llwrtgr4h` | [profile](https://agentverse.ai/agents/details/agent1q2yevx2vgmj7csvul5s7e7yucaf2l6gxzw9708vvc2ftjltye0llwrtgr4h/profile) |
| ShopBot-Leaky | demo target (asi1 brain + 4 store tools) | `agent1qv79tyq956x0ukz0l826zukgcvmtv7ywd9qn90cygx2yauhwq4md2g795vs` | [profile](https://agentverse.ai/agents/details/agent1qv79tyq956x0ukz0l826zukgcvmtv7ywd9qn90cygx2yauhwq4md2g795vs/profile) |
| ShopBot-Overcharge | demo target (asi1 brain + 4 store tools) | `agent1qtkvsq293xkt9eeq43u4zm4f4qte5wehq62jt7h897tlfflg3v9k56p9y5u` | [profile](https://agentverse.ai/agents/details/agent1qtkvsq293xkt9eeq43u4zm4f4qte5wehq62jt7h897tlfflg3v9k56p9y5u/profile) |
| ShopBot-Solid | demo target (asi1 brain + 4 store tools) | `agent1qdxscp2c0fkqgu3yd2q04wz4j6t0lewhghx7gmp8jdx9sh0dgkt7j5wjuca` | [profile](https://agentverse.ai/agents/details/agent1qdxscp2c0fkqgu3yd2q04wz4j6t0lewhghx7gmp8jdx9sh0dgkt7j5wjuca/profile) |

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

## Going live

`uv run python live.py init-env && uv run python live.py check && uv run python live.py register && uv run python run.py`. Details in `docs/LIVE.md`. To keep it running until Oct 28 on an AWS Lightsail VM: `deploy/lightsail.sh && deploy/deploy.sh` (see `docs/DEPLOY.md`).

## Scope

Hackathon scope, not SaaS: one process, one SQLite file, no login, no web dashboard, test funds only, opt-in allowlist.

## Team and layout

See `AGENTS.md` (ownership, rules, commands) and `docs/STATUS.md` (what's merged, what's next).
