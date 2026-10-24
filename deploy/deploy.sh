#!/usr/bin/env bash
# Ship a commit and the root .env to the VM and (re)start Canary. Run from your machine, from the repo root:
#   deploy/deploy.sh                 # deploys origin/main to the host in deploy/.host
#   REF=<commit> deploy/deploy.sh    # deploy a specific commit
#   HOST=ubuntu@1.2.3.4 deploy/deploy.sh
# The code is exported with `git archive`, so the server needs no repo credentials and gets exactly that commit.
# .env (keys + seeds) is streamed over ssh into /opt/canary/.env (mode 600) and never touches git.
set -euo pipefail

cd "$(dirname "$0")/.."
HOST="${HOST:-$(cat deploy/.host 2>/dev/null || true)}"
REF="${REF:-origin/main}"
KEY="${SSH_KEY:-$HOME/.ssh/canary-lightsail.pem}"
ENV_FILE="${ENV_FILE:-}"

[ -n "$HOST" ] || { echo "no host: run deploy/lightsail.sh first, or set HOST=user@ip" >&2; exit 1; }
if [ -z "$ENV_FILE" ]; then   # the nearest .env at or above this folder (the repo root's, also from a worktree)
  dir="$PWD"
  while [ "$dir" != "/" ] && [ ! -f "$dir/.env" ]; do dir="$(dirname "$dir")"; done
  ENV_FILE="$dir/.env"
fi
[ -f "$ENV_FILE" ] || { echo "no .env found at or above $PWD" >&2; exit 1; }
if grep -qE '^LOCAL_ONLY=(1|true)' "$ENV_FILE"; then echo ".env has LOCAL_ONLY=1: refusing to deploy a dev config" >&2; exit 1; fi
for k in ASI1_API_KEY AGENTVERSE_API_KEY; do
  grep -qE "^$k=.+" "$ENV_FILE" || { echo "$k is empty in $ENV_FILE: fill it in first (uv run python live.py check)" >&2; exit 1; }
done

SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new)
git fetch origin main --quiet || true
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
git archive --format=tar "$REF" | tar -x -C "$STAGE"
echo "deploying $(git rev-parse --short "$REF") to $HOST"

rsync -az --delete -e "ssh -i $KEY -o StrictHostKeyChecking=accept-new" --rsync-path="sudo -u canary rsync" \
  --exclude .venv --exclude .env --exclude '*.db' "$STAGE"/ "$HOST":/opt/canary/

"${SSH[@]}" "$HOST" 'sudo -u canary tee /opt/canary/.env >/dev/null && sudo chmod 600 /opt/canary/.env' < "$ENV_FILE"

"${SSH[@]}" "$HOST" 'sudo -u canary bash -lc "cd /opt/canary && \$HOME/.local/bin/uv sync --frozen --no-dev"'
"${SSH[@]}" "$HOST" 'sudo install -m 644 /opt/canary/deploy/canary.service /etc/systemd/system/canary.service \
  && sudo systemctl daemon-reload && sudo systemctl enable canary >/dev/null 2>&1 && sudo systemctl restart canary'

sleep 8
"${SSH[@]}" "$HOST" 'systemctl is-active canary && sudo journalctl -u canary -n 15 --no-pager | cut -c1-180'
echo "follow the logs:  ssh -i $KEY $HOST 'sudo journalctl -u canary -f'"
