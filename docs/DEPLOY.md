# Deploy: one always-on VM until results on Oct 28

Canary is one process (`run.py`: eight agents in a Bureau) with the Orchestrator on the Agentverse mailbox. It makes **outbound connections only**
(Agentverse mailbox, asi1, Dodo), keeps state in one SQLite file, and must exist **once**: the same seeds running on two machines share an
address and break delivery. So: one small AWS Lightsail VM (Ubuntu 24.04, 2 GB, about $12/month, SSH-only firewall) running it under systemd.

Serverless hosts (Vercel etc.) cannot run it: it is a long-lived process that polls a mailbox.

## Before you deploy (gates)
- [ ] Root `.env` complete: `ASI1_API_KEY`, `AGENTVERSE_API_KEY`, `DODO_PAYMENTS_API_KEY`, `DODO_PRODUCT_ID`, `BACKBOARD_API_KEY`, all 8 `SEED_*`, `LOCAL_ONLY=0`.
- [ ] `uv run python live.py check` shows no `FAIL`; `uv run pytest -m live` green.
- [ ] Codex PRs merged into `main` and the 3x3 matrix is green twice (leaky F, overcharger F, solid A). Deploy before this and the demo runs on the dev stubs.
- [ ] Nothing else is running these seeds: stop any local `python run.py` started with this `.env`.
- [ ] `uv run python live.py register` done once (creates the 8 Agentverse profiles; paste the addresses and URLs into the README).

## First deploy
```bash
deploy/lightsail.sh     # creates the VM + static IP + SSH-only firewall, runs deploy/bootstrap.sh; writes deploy/.host (git-ignored)
deploy/deploy.sh        # ships origin/main + the root .env (mode 600) and starts the systemd service
```
`lightsail.sh` needs the `aws` CLI authenticated with Lightsail rights (checked first with a read-only call). SSH is allowed only from your current IP; set
`SSH_CIDR=0.0.0.0/0` to open it up. Settings you can override: `NAME`, `AWS_REGION`, `BUNDLE`, `SSH_KEY`.

## Check it is healthy
```bash
ssh -i ~/.ssh/canary-lightsail.pem "$(cat deploy/.host)" 'sudo journalctl -u canary -f'   # look for: Bureau up: ... (8 agents)
uv run python live.py status                                                              # 8/8 agents healthy
```
Then in ASI:One: find Canary-Orchestrator, send `hello` (must answer in under 10 s), `Is ShopBot-Solid safe to pay?`, and **save the shared chat link**.
Run one real Dodo test-mode payment end to end (`docs/LIVE.md` section 7) and confirm a replay is refused.
Reboot once (`sudo reboot`) and confirm the service comes back on its own.

## Redeploy (after every merge)
```bash
deploy/deploy.sh                 # origin/main
REF=<commit> deploy/deploy.sh    # a specific commit
```
About 10 seconds of downtime. The DB lives in `/var/lib/canary/canary.db`, outside the code tree, so redeploys never touch it. The deploy script refuses a `.env`
with `LOCAL_ONLY=1` or a missing `ASI1_API_KEY` / `AGENTVERSE_API_KEY`.

## Operations
| Task | Command (after `ssh -i ~/.ssh/canary-lightsail.pem "$(cat deploy/.host)"`) |
|---|---|
| Is it running | `systemctl is-active canary` |
| Live logs | `sudo journalctl -u canary -f` |
| Last errors | `sudo journalctl -u canary -p err -n 50 --no-pager` |
| Restart | `sudo systemctl restart canary` |
| Back up the DB | `sudo cp /var/lib/canary/canary.db ~/canary-$(date +%F).db` |
| Snapshot the VM | `aws lightsail create-instance-snapshot --instance-name canary --instance-snapshot-name canary-demo` |

## If the Orchestrator never connects to the mailbox
`register` creates the agents through the Agentverse API; if the mailbox does not attach (logs show no mailbox connection and ASI:One never reaches it), connect it
the classic way: open an SSH tunnel to the inspector (`ssh -L 8100:127.0.0.1:8100 ...`), open the Orchestrator's inspector link from the logs in your browser,
choose **Connect > Mailbox**, then restart the service.

## Teardown (after Oct 28)
```bash
deploy/lightsail.sh destroy      # deletes the instance, static IP and key pair (asks first)
```
Keep a copy of the DB and of the seeds if you want the same agent addresses later.

## Security notes
- Keys and seeds exist only in the root `.env` and on the VM (`/opt/canary/.env`, mode 600, owned by the `canary` service user). They never go through git.
- The service runs as an unprivileged `canary` user with `NoNewPrivileges` and a read-only system (`ProtectSystem=full`).
- No inbound port except SSH; there is no public HTTP surface to attack.
