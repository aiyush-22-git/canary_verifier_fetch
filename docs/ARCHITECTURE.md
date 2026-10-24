# Canary architecture (agentic, asi1-based)

Canary is a multi-agent trust auditor for Agentverse agents. A user asks in ASI:One "is X safe to pay?"; Canary plans an
audit with asi1, attacks the target live with three probe agents over the real Chat and Payment Protocols, grades the
transcripts (deterministic rules first, asi1 only for soft cases), and returns a scorecard card. Verified agents are
re-tested on a timer.

## 1. System view

```
                    ┌──────────────────────── ASI:One (asi1 chat UI) ───────────────────────┐
                    │  user: "Is ShopBot-Overcharge safe to pay?"                            │
                    └──────────────┬───────────────────────────────────────▲─────────────────┘
                     Chat Protocol │ + Payment Protocol (seller)           │ cards: plan, payment link,
                     (Agentverse)  ▼                                       │ progress, scorecard, findings
┌───────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ Canary-Orchestrator            agents/orchestrator.py                                                 │
│  intent   core/intent.py   asi1 tool calling → lookup | audit | verify_owner | prose answer           │
│  planner  core/planner.py  asi1 JSON → picks/varies attack cases from packs/*.yaml                    │
│  cards    core/cards.py    ASI:One cards (audit_plan, payment_link, scorecard, finding_detail, notice)│
│  payments core/payments.py Dodo checkout | FET ledger | Stripe; server-side verify; replay rejected    │
│  registry core/registry.py SQLite (hosted: agent storage snapshot) — agents, audits, verdicts, tx ids  │
│  timers   sweep (10 s watchdog) · reverify (REVERIFY_INTERVAL_S) for owner-verified agents            │
└───────┬─────────────────────────────── ProbeRequest x3 ─────────────────────────────┬─────────────────┘
        │ JudgeRequest                                                                │
        ▼                                                                             ▼
┌───────────────────┐   ProbeResult (Exchange transcripts)    ┌───────────────────────────────────────────┐
│ Canary-Judge      │◄────────────────────────────────────────│ Canary-Probe-Injection / -Leak / -Payment │
│ agents/judge.py   │                                         │ agents/probe.py (one class, 3 packs)      │
│ hard rules (regex,│── ProbeProgress, Verdict ──► Orchestr.  │ no LLM: deterministic state machine       │
│ events, ack, amt) │                                         └───────────────┬───────────────────────────┘
│ + asi1 soft grade │                                    Chat + Payment Proto │ (probe = buyer)
└───────────────────┘                                                         ▼
                                                  ┌───────────────────────────────────────────────────┐
                                                  │ ShopBot-Leaky / -Overcharge / -Solid               │
                                                  │ agents/target.py · brain = asi1 tool loop          │
                                                  │ tools targets/tools.py over targets/store.json     │
                                                  │ behaviour = data in targets/profiles.yaml          │
                                                  └───────────────────────────────────────────────────┘
```

Two deployment shapes share the same code; the user interface is ASI:One itself:

| Mode | How | Network |
|---|---|---|
| Hosted (submission) | `deploy/hosted/build.py` bundles each agent into one self-contained `agent.py` pasted into an Agentverse hosted agent. `hosted_addresses.py` + `hosted_secrets.py` are embedded; `config.HOSTED` is true | every hop goes over Agentverse; registry lives in the agent's key-value storage |
| Bureau | `run.py` runs all 8 agents in one uAgents Bureau; only the Orchestrator uses the mailbox | internal hops are local |

## 2. Where asi1 is used (and where it is not)

All calls go through `core/llm.py`: an `AsyncOpenAI` client on `https://api.asi1.ai/v1`, model `ASI1_MODEL` (default
`asi1`, fallback `asi1-mini`). A global `LLM_TOKEN_CAP` stops runaway spend.

| Where | Function | asi1 call | Without asi1 |
|---|---|---|---|
| Orchestrator intent | `core/intent.py: classify` | `llm.tool_call`: asi1 picks a tool (`check_trust`, `start_audit`, `verify_owner`) or answers in prose | `keyword_intent` regex fallback |
| Audit planning | `core/planner.py: plan_audit` | `llm.complete_json`: reads the target's Agentverse profile, picks and rewrites attack cases within budget | `default_pick` by priority, every family kept |
| Soft grading | `agents/judge.py: grade_soft` | `llm.complete_json`: grades a soft prompt-injection case against its rubric. It must quote the transcript verbatim, and the quote is checked | case reported as ungraded, never passed |
| Demo targets' brain | `agents/target.py` → `llm.run_tools` | full tool loop on `ASI1_TARGET_MODEL`: asi1 calls `search_catalog`, `get_order`, `apply_discount`, `issue_refund` | rule-based reply |
| Probes | `agents/probe.py` | **none**: attacks are deterministic so results are reproducible | — |
| Hard grading | `agents/judge.py: evaluate_rule` | **none**: canary token string match, event regex, ack check, amount check | — |

Design rule: **a grade never rests on a model's opinion when a hard rule applies.** Any hard fail is an F. asi1 only
decides the soft cases, and it must cite evidence.

## 3. One paid audit, end to end

1. **ASI:One → Orchestrator** `ChatMessage("Is ShopBot-Overcharge safe to pay?")`. The Orchestrator ACKs it.
2. **Intent (asi1)** → `audit` + agent ref; `intent.resolve_agent` maps the name to an address via the registry. A plain
   lookup is answered free from the registry: a finished verdict younger than `VERDICT_FRESH_S` (default 1 h) is shown
   with its audit id and age (`cards.trust_lookup`); an older one is labelled historical and the fresh audit plan follows
   (`cards.historical_lookup`). Partial verdicts always carry their reason; verdicts that graded nothing are never shown.
3. **Consent gate**: only demo targets and owner-verified agents (a one-time code put in their Agentverse profile) are
   tested.
4. **Plan (asi1)** → `AuditPlan` → `cards.audit_plan` with *Pay and run* / *Cancel*.
5. **Payment**: `PAYMENT_RAIL=dodo` sends a hosted checkout link card. *I've paid* → `payments.verify_dodo` asks Dodo
   server-side (status, this session, amount, currency). `fet`/`stripe` use `RequestPayment` → `CommitPayment` →
   `verify_commit`. Transaction ids are single-use, so a replay gets `CancelPayment`. With `free`, no payment is asked
   for and none is faked.
6. **Run**: `JudgeRequest` → Judge; `ProbeRequest` per kind → three probes. Each probe drives one case at a time per
   target (lock + `PROBE_TIMEOUT_S`) and records an `Exchange` (sent, received, events, acked, latency, timed_out).
7. **Probes → Judge** `ProbeResult`. The Judge streams `ProbeProgress` lines and the Orchestrator relays them to the
   user live.
8. **Verdict**: hard rules, then asi1 for soft cases, then a letter grade. The registry stores it and the Orchestrator
   sends `cards.scorecard`, with per-finding `finding_detail` (verbatim quote + fix) on demand.
9. **Watchdog/timeout**: `sweep` ends an audit at `AUDIT_TIMEOUT_S`. If every case was skipped, the user gets a
   "couldn't test" notice and nothing is cached.
10. **Re-verify**: `reverify` re-runs verified agents on a timer. A regression updates the registry.

## 4. Attack families (packs/*.yaml → `AttackCase`)

| Pack | Probe | Example attacks | Rule kinds |
|---|---|---|---|
| `injection.yaml` | Probe-Injection | direct + tool-output injection (poisoned catalog row), raw JSON card-selection spoof `{"action":"confirm"}` | `text_match`, `soft` |
| `leak.yaml` | Probe-Leak | system-prompt / canary token extraction, another customer's order (`ORD-2002`, `bob@example.com`) | `text_match` with `{canary}` |
| `payment.yaml` | Probe-Payment | `pay_fake`, `pay_replay`, `pay_wrong_amount`, `service_before_pay`, injected 100 % discount | `event_match`, `amount_below` |
| (all) | — | every chat message must be ACKed | `ack_missing` |

Expected matrix (each demo target's system prompt holds its own canary token):

| | Injection | Leak | Payment | Grade |
|---|---|---|---|---|
| ShopBot-Leaky | fail (2 cases) | fail | pass | F |
| ShopBot-Overcharge | fail (5 cases) | fail (1 case) | fail | F |
| ShopBot-Solid | pass | pass | pass | A |

## 5. Contracts (`core/models.py`)

`AttackCase → Pack`, `TargetProfile`, `ProbeRequest → Exchange → ProbeResult`, `JudgeRequest`, `ProbeProgress`,
`Finding → ProbeSummary → Verdict`, `AuditPlan`, `RegistryEntry`. These are uAgents `Model`s and the only messages
exchanged between Canary agents; Chat and Payment Protocol messages are used toward ASI:One and the targets.

## 6. Configuration

`core/config.py` reads, in order, the hosted bundle's embedded `hosted_secrets.SECRETS`, then the environment
(`.env`), then the defaults. Key variables: `ASI1_API_KEY`, `ASI1_MODEL`, `ASI1_TARGET_MODEL`, `LLM_TOKEN_CAP`,
`AGENTVERSE_API_KEY`, `SEED_*` (one per agent), `PAYMENT_RAIL` (`dodo|fet|stripe|free|none`), `DODO_PAYMENTS_API_KEY`,
`DODO_PRODUCT_ID`, `PROBE_TIMEOUT_S`, `AUDIT_TIMEOUT_S`, `REVERIFY_INTERVAL_S`, `VERDICT_FRESH_S`. See `.env.example`.

## 7. Ship checklist

```bash
uv sync
uv run python live.py check                    # asi1 ping, seeds, Agentverse, payment rail
uv run pytest -m live -v                       # real asi1: intent tools, planner, soft grading
uv run python deploy/hosted/build.py --addresses deploy/hosted/addresses.json
python -I deploy/hosted/smoke.py deploy/hosted/dist/judge/agent.py
# paste dist/<agent>/agent.py into each hosted agent: targets, probes, Judge, Orchestrator last
```
Then in ASI:One: `hello` → `Is ShopBot-Solid safe to pay?` → save the shared chat link.
