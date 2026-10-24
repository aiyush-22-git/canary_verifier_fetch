#!/usr/bin/env bash
# One-time VM setup (idempotent). Run on a fresh Ubuntu 24.04 host, as root:
#   ssh ubuntu@HOST 'sudo bash -s' < deploy/bootstrap.sh
# Installs the OS packages, a `canary` service user, uv, and the data directory. The code, .env and
# the systemd unit arrive with deploy/deploy.sh.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y rsync curl ca-certificates

id canary >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash canary
install -d -o canary -g canary -m 755 /opt/canary
install -d -o canary -g canary -m 750 /var/lib/canary   # SQLite lives here, outside the code tree

# uv (the project's package manager) for the service user; it also fetches Python 3.12 on first sync.
if ! sudo -u canary bash -lc 'test -x "$HOME/.local/bin/uv"'; then
  sudo -u canary bash -lc 'curl -LsSf https://astral.sh/uv/install.sh | sh'
fi

# A little swap so a memory spike during `uv sync` can't kill the box.
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "bootstrap done: now run deploy/deploy.sh from your machine"
