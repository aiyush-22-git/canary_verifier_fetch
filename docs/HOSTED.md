# Run Canary on Agentverse (hosted agents, copy and paste)

No server. Each of the 8 agents is a hosted agent on Agentverse; you paste one generated file into each.

## 1. Create the 8 empty agents
On https://agentverse.ai: **Agents > Launch an Agent > Hosted agent (blank)**. Create these names (the names are what ASI:One shows):

`Canary-Orchestrator`, `Canary-Judge`, `Canary-Probe-Injection`, `Canary-Probe-Leak`, `Canary-Probe-Payment`, `ShopBot-Leaky`, `ShopBot-Overcharge`, `ShopBot-Solid`

Copy each agent's address (`agent1q...`) into `deploy/hosted/addresses.json` (git-ignored):

```json
{"judge": "agent1q...", "probe-injection": "agent1q...", "probe-leak": "agent1q...", "probe-payment": "agent1q...",
 "target-leaky": "agent1q...", "target-overcharger": "agent1q...", "target-solid": "agent1q..."}
```
(The Orchestrator is the only agent that needs the others' addresses.)

## 2. Generate the files
```bash
uv run python deploy/hosted/build.py --addresses deploy/hosted/addresses.json
```
This writes `deploy/hosted/dist/<agent>/agent.py` (30 to 75 KB each) with your keys from the root `.env` inside, so **do not share or commit
them** (`dist/` is git-ignored). Each file is self-contained: no other files to upload.
Check one without Agentverse: `cd /tmp && python -I <repo>/deploy/hosted/smoke.py <repo>/deploy/hosted/dist/judge/agent.py`.

## 3. Paste and start
For each agent: open it > **Build** tab > replace the whole content of `agent.py` with the matching generated file > **Save** > **Start**.
Order: targets, probes, Judge, then the Orchestrator last. Paste `dist/<agent>/README.md` into the agent's README tab if it exists.
Look at the **Logs** of each agent after Start: there must be no traceback.

## 4. Test
In ASI:One (or Agentverse chat with Canary-Orchestrator): `hello`, then `Is ShopBot-Solid safe to pay?`. For a paid audit use the Dodo test card.
Save the shared chat link and the 8 profile URLs for the submission.

## If something fails
- `ModuleNotFoundError` / import error in the logs: the hosted runtime lacks a package; send me the log line.
- Size warning from build.py: the hosted editor has a code-size limit; the builder already compresses any file over 100 KB.
- Orchestrator says "payments aren't available": check `DODO_PAYMENTS_API_KEY` / `DODO_PRODUCT_ID` in `.env`, rebuild, re-paste.
- Never run `python run.py` at the same time with the same seeds: hosted agents have their own addresses, but the old mailbox agents would answer too.
