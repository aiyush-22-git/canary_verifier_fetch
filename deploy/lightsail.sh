#!/usr/bin/env bash
# Create the Canary VM on AWS Lightsail (idempotent: re-running skips what exists).
#   deploy/lightsail.sh            # create + bootstrap
#   deploy/lightsail.sh destroy    # delete the instance, key pair and static IP (do this after Oct 28)
# Needs the aws CLI authenticated with Lightsail permissions. One small Ubuntu box, SSH-only firewall:
# Canary only makes outbound connections (Agentverse mailbox, asi1, Dodo), so no other port is opened.
set -euo pipefail

cd "$(dirname "$0")/.."
REGION="${AWS_REGION:-us-east-1}"
NAME="${NAME:-canary}"
KEYNAME="${KEYNAME:-canary-key}"
PEM="${SSH_KEY:-$HOME/.ssh/canary-lightsail.pem}"
BUNDLE="${BUNDLE:-small_3_0}"         # 2 GB RAM, about $12/month
BLUEPRINT="ubuntu_24_04"
STATIC_IP="${NAME}-ip"
LS=(aws lightsail --region "$REGION")

if [ "${1:-}" = "destroy" ]; then
  read -r -p "Delete instance $NAME, static IP $STATIC_IP and key pair $KEYNAME? [y/N] " yn
  [ "$yn" = "y" ] || exit 1
  "${LS[@]}" release-static-ip --static-ip-name "$STATIC_IP" 2>/dev/null || true
  "${LS[@]}" delete-instance --instance-name "$NAME" 2>/dev/null || true
  "${LS[@]}" delete-key-pair --key-pair-name "$KEYNAME" 2>/dev/null || true
  rm -f deploy/.host
  echo "deleted"
  exit 0
fi

"${LS[@]}" get-bundles >/dev/null   # fails fast if this IAM user has no Lightsail permission

if ! "${LS[@]}" get-instance --instance-name "$NAME" >/dev/null 2>&1; then
  if [ ! -f "$PEM" ]; then
    mkdir -p "$(dirname "$PEM")"
    "${LS[@]}" create-key-pair --key-pair-name "$KEYNAME" --query privateKeyBase64 --output text > "$PEM"
    chmod 600 "$PEM"
  fi
  "${LS[@]}" create-instances --instance-names "$NAME" --availability-zone "${REGION}a" \
    --blueprint-id "$BLUEPRINT" --bundle-id "$BUNDLE" --key-pair-name "$KEYNAME" >/dev/null
  echo "created $NAME ($BUNDLE), waiting for it to run..."
fi
until [ "$("${LS[@]}" get-instance --instance-name "$NAME" --query instance.state.name --output text)" = "running" ]; do sleep 5; done

"${LS[@]}" get-static-ip --static-ip-name "$STATIC_IP" >/dev/null 2>&1 || "${LS[@]}" allocate-static-ip --static-ip-name "$STATIC_IP" >/dev/null
"${LS[@]}" attach-static-ip --static-ip-name "$STATIC_IP" --instance-name "$NAME" >/dev/null 2>&1 || true
IP="$("${LS[@]}" get-static-ip --static-ip-name "$STATIC_IP" --query staticIp.ipAddress --output text)"

# SSH only, from this machine unless SSH_CIDR says otherwise.
MY_IP="$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')"
CIDR="${SSH_CIDR:-$MY_IP/32}"
"${LS[@]}" put-instance-public-ports --instance-name "$NAME" \
  --port-infos "fromPort=22,toPort=22,protocol=tcp,cidrs=$CIDR" >/dev/null

echo "ubuntu@$IP" > deploy/.host
echo "waiting for SSH on $IP..."
until ssh -i "$PEM" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 "ubuntu@$IP" true 2>/dev/null; do sleep 5; done
ssh -i "$PEM" "ubuntu@$IP" 'sudo bash -s' < deploy/bootstrap.sh
echo
echo "VM ready: ubuntu@$IP   (SSH allowed from $CIDR)"
echo "next:  deploy/deploy.sh"
