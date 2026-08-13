#!/usr/bin/env bash
# Generate a read-only deploy key inside the VM for one private repo.
#
#   ./bootstrap/make-deploy-key.sh <vm-ip> <owner/repo> [host]
#
#   ./bootstrap/make-deploy-key.sh 192.168.122.167 tibber/some-service
#
# Prints a public key for you to add on GitHub under
#   Settings -> Deploy keys -> Add deploy key
# with "Allow write access" LEFT UNCHECKED.
#
# One key per repo, deliberately. GitHub rejects the same deploy key on a second
# repository, so a shared key cannot work. Each key therefore gets its own Host
# alias in the agent's ~/.ssh/config, and you clone via that alias:
#
#   git@github.com-some-service:tibber/some-service.git
#
# The private key is generated inside the guest and never leaves it. Only the
# public half is printed.
set -euo pipefail

VM_HOST="${1:-}"
REPO_SLUG="${2:-}"
GIT_HOST="${3:-github.com}"
ADMIN_KEY="${ADMIN_KEY:-$HOME/.ssh/agent_vm_admin_ed25519}"

if [[ -z "$VM_HOST" || -z "$REPO_SLUG" ]]; then
    echo "usage: $0 <vm-ip> <owner/repo> [host]" >&2
    exit 64
fi

if [[ "$REPO_SLUG" != */* ]]; then
    echo "ERROR: expected owner/repo, got '$REPO_SLUG'" >&2
    exit 64
fi

# Alias and key name derived from the repo, so several repos coexist.
REPO_NAME="${REPO_SLUG##*/}"
SAFE_NAME="$(printf '%s' "$REPO_NAME" | tr -cd '[:alnum:]._-')"
ALIAS="${GIT_HOST}-${SAFE_NAME}"
KEY_PATH="/home/agent/.ssh/deploy_${SAFE_NAME}"

SSH=(ssh -i "$ADMIN_KEY" -o IdentitiesOnly=yes -o BatchMode=yes
     -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "admin@$VM_HOST")

echo "==> Preparing the agent's ssh setup"
"${SSH[@]}" "GIT_HOST='$GIT_HOST' ALIAS='$ALIAS' KEY_PATH='$KEY_PATH' \
             REPO_SLUG='$REPO_SLUG' bash -s" <<'REMOTE'
set -eu

sudo install -d -o agent -g agent -m 0700 /home/agent/.ssh

if sudo test -f "$KEY_PATH"; then
    echo "    reusing the existing key at $KEY_PATH"
else
    sudo -u agent -H ssh-keygen -t ed25519 -N '' \
        -C "agent-vm deploy key for $REPO_SLUG" -f "$KEY_PATH" >/dev/null
    echo "    generated $KEY_PATH"
fi

# Pin the host key rather than trusting on first use: the agent runs unattended,
# so there is no human to eyeball a fingerprint prompt.
if ! sudo -u agent test -s /home/agent/.ssh/known_hosts \
   || ! sudo -u agent grep -q "$GIT_HOST" /home/agent/.ssh/known_hosts 2>/dev/null; then
    ssh-keyscan -t rsa,ecdsa,ed25519 "$GIT_HOST" 2>/dev/null \
        | sudo -u agent tee -a /home/agent/.ssh/known_hosts >/dev/null
    echo "    added $GIT_HOST host keys to the agent's known_hosts"
fi

# An idempotent per-repo Host block. IdentitiesOnly stops ssh offering every key
# it can find, which GitHub answers by authenticating as whichever repo the first
# accepted key belongs to — a confusing failure when several deploy keys exist.
CONFIG=/home/agent/.ssh/config
sudo touch "$CONFIG"
if ! sudo grep -q "^Host $ALIAS\$" "$CONFIG" 2>/dev/null; then
    sudo tee -a "$CONFIG" >/dev/null <<EOF

Host $ALIAS
    HostName $GIT_HOST
    User git
    IdentityFile $KEY_PATH
    IdentitiesOnly yes
EOF
    echo "    added Host $ALIAS"
else
    echo "    Host $ALIAS already configured"
fi
sudo chown -R agent:agent /home/agent/.ssh
sudo chmod 0600 "$CONFIG" "$KEY_PATH"
sudo chmod 0644 "$KEY_PATH.pub" /home/agent/.ssh/known_hosts
REMOTE

echo
echo "==> Checking the guest can reach $GIT_HOST on port 22"
if "${SSH[@]}" "timeout 8 bash -c 'cat </dev/null >/dev/tcp/$GIT_HOST/22'" 2>/dev/null; then
    echo "    reachable"
else
    echo "    NOT reachable — the egress policy or DNS is blocking it" >&2
fi

echo
echo "============================================================"
echo "Add this as a deploy key on https://github.com/$REPO_SLUG"
echo "  Settings -> Deploy keys -> Add deploy key"
echo "  Leave 'Allow write access' UNCHECKED."
echo "============================================================"
"${SSH[@]}" "sudo cat '$KEY_PATH.pub'"
echo "============================================================"
echo
echo "Then clone it with the alias (not plain github.com):"
echo "  ./bootstrap/add-repo.sh $VM_HOST git@$ALIAS:$REPO_SLUG.git $SAFE_NAME"
echo
echo "To confirm the key works once you have added it:"
echo "  ./bootstrap/make-deploy-key.sh $VM_HOST $REPO_SLUG --verify-only 2>/dev/null || true"
echo "  ssh -i $ADMIN_KEY admin@$VM_HOST \\"
echo "    \"sudo -u agent -H ssh -o IdentitiesOnly=yes -i $KEY_PATH -T git@$ALIAS\""
echo "  (expect: 'Hi $REPO_SLUG! You've successfully authenticated, but GitHub"
echo "   does not provide shell access.')"
