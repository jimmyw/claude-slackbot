#!/usr/bin/env bash
# Set up the GitHub machine-user key and the daemon's ssh-agent.
#
#   ./bootstrap/50-machine-user-key.sh <vm-ip>
#
# Generates a passphrase-less ed25519 key ON THE HOST, installs the ssh-agent
# unit, loads the key with a destination constraint, and permits agent forwarding
# for the daemon's key in the guest.
#
# The private key never reaches the VM. The guest can only ask the host's agent to
# sign a challenge, and only for the one hop `agent@vm > git@github.com`, and only
# while a run is in flight. Compromise the guest later and there is nothing at
# rest to steal.
#
# Why a machine user and not your own account: a GitHub SSH key on a user account
# inherits everything that user can do, including push to every repo they can
# write. GitHub has no read-only flag for user keys. Read-only has to come from a
# separate account with read-only org/team access.
set -euo pipefail

VM_HOST="${1:-}"
KEY="${MACHINE_USER_KEY:-$HOME/.ssh/github_machine_user_ed25519}"
ADMIN_KEY="${ADMIN_KEY:-$HOME/.ssh/agent_vm_admin_ed25519}"
STATE_DIR="$HOME/.local/share/slack-claude"
UNIT_DIR="$HOME/.config/systemd/user"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -z "$VM_HOST" ]]; then
    echo "usage: $0 <vm-ip>" >&2
    exit 64
fi

SSH=(ssh -F /dev/null -i "$ADMIN_KEY" -o IdentitiesOnly=yes -o BatchMode=yes
     -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "admin@$VM_HOST")

echo "==> Generating the machine-user key on the host"
mkdir -p "$STATE_DIR" "$UNIT_DIR"
if [[ -f "$KEY" ]]; then
    echo "    reusing $KEY"
else
    ssh-keygen -t ed25519 -N '' -C "agent-vm machine user $(hostname)" -f "$KEY" >/dev/null
    echo "    generated $KEY"
fi
chmod 0600 "$KEY"

echo "==> Permitting agent forwarding for the daemon's key in the guest"
# The daemon key was pinned with no-agent-forwarding. Forwarding is the whole
# mechanism here, so that option has to go; the forced command and no-pty stay,
# so the key still cannot open a shell.
timeout 40 "${SSH[@]}" 'bash -s' <<'REMOTE'
set -eu
AK=/home/agent/.ssh/authorized_keys
if sudo grep -q 'no-agent-forwarding' "$AK"; then
    sudo cp "$AK" "$AK.bak.$(date +%s)"
    sudo sed -i 's/,no-agent-forwarding//; s/no-agent-forwarding,//' "$AK"
    echo "    removed no-agent-forwarding"
else
    echo "    already permitted"
fi
echo -n "    daemon key options now: "
sudo grep -o 'command="[^"]*"[^ ]*' "$AK" | head -1
REMOTE

echo "==> Installing the ssh-agent unit"
install -m 0644 "$REPO_DIR/daemon/systemd/slack-claude-ssh-agent.service" "$UNIT_DIR/"
install -m 0644 "$REPO_DIR/daemon/systemd/slack-claude-daemon.service" "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable --now slack-claude-ssh-agent.service
systemctl --user restart slack-claude-daemon.service

echo
echo "==> Agent state"
SSH_AUTH_SOCK="$STATE_DIR/ssh-agent.sock" ssh-add -l 2>&1 | sed 's/^/    /' || true

echo
echo "============================================================"
echo "Add this to the MACHINE USER's GitHub account:"
echo "  https://github.com/settings/keys  (logged in as the machine user)"
echo
echo "Then give that account read-only access to the repos:"
echo "  tibber org -> Teams -> a read-only team, or per-repo Read permission"
echo "============================================================"
cat "$KEY.pub"
echo "============================================================"
echo
echo "Confirm once it is added and has org access:"
echo "  ./bootstrap/verify-agent-forwarding.sh $VM_HOST"
