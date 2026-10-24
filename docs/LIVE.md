# Live runbook: from clone to a paid audit inside ASI:One

Run this on **one machine only** (A1's). Codex machines stay on `LOCAL_ONLY=1`. About 20 minutes the first time.

## 0. Prerequisites
- Accounts: Agentverse, ASI:One, an asi1 API key, an Agentverse API key (Agentverse -> Profile -> API keys). Stripe test keys only if you chose Stripe.
- `uv sync`

## 1. Create `.env` (generates the secrets for you)
```
uv run python live.py init-env      # writes .env with a fresh random seed per agent, LOCAL_ONLY=0, mode 600
```
Open `.env` and add `ASI1_API_KEY` and `AGENTVERSE_API_KEY`. **Back up the seeds**: a seed
is an agent's identity; lose it and you lose its Agentverse address.

## 2. Preflight
```
uv run python live.py check
```
It checks seeds, pings asi1 for real, checks Agentverse reachability, the payment rail, and prints the Orchestrator's seller wallet. Everything
`FAIL` must be fixed before continuing.

## 3. Live brain tests (real asi1, no Agentverse needed)
```
uv run pytest -m live -v            # intent tool calling, planner variants, Judge soft grading with verbatim quotes
E2E_LIVE=1 uv run pytest tests/test_e2e.py -q   # the whole Bureau flow with real asi1 instead of the offline fallbacks
```
`-m live` is skipped by default and in CI; it only needs `ASI1_API_KEY`.

## 4. Register the 8 agents on Agentverse
```
uv run python live.py register --dry-run   # shows what will be sent, no network
uv run python live.py register             # registers all 8, prints each Agentverse profile URL
```
Paste the printed addresses and profile URLs into the README table. Each agent's README (`docs/agentverse/*.md`) is its profile text; ASI:One
discovery ranks on it, so keep the keywords.

## 5. Start the Bureau
```
uv run python run.py
```
Look for these lines: `Bureau up: ...` (8 agents), and the Orchestrator's mailbox connecting. Keep this process running (a machine that won't
sleep: `caffeinate -i uv run python run.py` on macOS) until results on Oct 28.

## 6. Prove the two mandatory items (Phase 1 gate)
1. In ASI:One, find **Canary-Orchestrator** (search "Canary", or start a message with `@` and the Orchestrator's address). Send `hello`: it must
   reply within 10 s with the help text.
2. Send `Is ShopBot-Solid safe to pay?`. You should get an audit plan card. Open the chat's share menu and **save the shared chat link**: it is a
   required deliverable.
3. If auto-routing never picks it, @mention the address instead and improve the README keywords later.

## 7. Payments with Dodo (default rail)
One-time setup in the Dodo dashboard (test mode): create an API key and **one product** (a one-time payment priced at or above
`AUDIT_PRICE_USD`, default 1.00 USD). Put `DODO_PAYMENTS_API_KEY` and `DODO_PRODUCT_ID` in `.env` and re-run `live.py check` (it fetches the product).

What the buyer sees in ASI:One: audit plan card -> **Pay and run** -> a message with the secure Dodo checkout link plus a card -> they pay in the
browser (Dodo test card: see Dodo's testing docs) -> **I've paid, start the audit** -> Canary asks Dodo (session, then payment: status `succeeded`,
belongs to *this* checkout, amount and currency) and only then starts the probes. The Dodo payment id is single-use, so a replay is refused.
No webhook or public URL is needed: Canary polls on the button press.

`DODO_SEND_REQUESTPAYMENT=1` additionally sends a Payment Protocol `RequestPayment` (method `dodo`, metadata holds the checkout link and session id) for
*agent* buyers: after paying they `CommitPayment` with the session id as `transaction_id`. Leave it off for human ASI:One chats.
Stripe (`PAYMENT_RAIL=stripe`) and FET (`fet`) remain as alternatives. Never fake a completed payment in the demo.

## 8. Paste the cards into the Playground
https://asi1.ai/developer/card-playground: paste each payload (print them with `uv run python -c "import json,tests.test_cards as t; [print(k, json.dumps(v.payload)) for k,v in t.SPECS.items()]"`).

## Troubleshooting
| Symptom | Fix |
|---|---|
| `live.py check` says asi1 unreachable | wrong key, quota, or offline; test with `curl https://api.asi1.ai/v1/models -H "Authorization: Bearer $ASI1_API_KEY"` |
| Orchestrator never replies in ASI:One | mailbox not connected (check the Bureau log), agent inactive on Agentverse, or two machines running the same seed |
| `register` says 401 | bad `AGENTVERSE_API_KEY` |
| "I can't see your payment yet" after paying | Dodo still `processing`; wait a few seconds and press the button again. Persistent: `DODO_ENV` doesn't match the key's mode |
| `register` says 409 | the address already belongs to another account: use fresh seeds |
| Audit stalls ~100 s then a "didn't finish" card | a probe or target is down; the card offers a free re-run |
