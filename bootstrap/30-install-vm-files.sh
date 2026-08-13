#!/usr/bin/env bash
# Push UPDATED guest-side files into a already-provisioned agent VM.
#
#   ./bootstrap/30-install-vm-files.sh <vm-ip>
#
# Not needed for a fresh VM: 10-provision-vm.sh embeds all of vm-files/ in the
# cloud-init seed, so the guest is fully provisioned on first boot. This script
# is for pushing changes afterwards.
#
# Uses the admin key, not the daemon key — the daemon key is pinned to a forced
# command and cannot run rsync or open a shell.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VM_HOST="${1:-}"
ADMIN_KEY="${ADMIN_KEY:-$HOME/.ssh/agent_vm_admin_ed25519}"

if [[ -z "$VM_HOST" ]]; then
    echo "usage: $0 <vm-ip>" >&2
    exit 64
fi

if [[ ! -f "$ADMIN_KEY" ]]; then
    echo "ERROR: no admin key at $ADMIN_KEY" >&2
    echo "It is created by 10-provision-vm.sh; set ADMIN_KEY to override." >&2
    exit 1
fi
SSH_OPTS=(-i "$ADMIN_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new)

echo "==> Copying vm-files to agent@$VM_HOST"
# tar rather than rsync: rsync is not installed in the guest and adding it just
# for this would be a package the agent never otherwise needs.
tar -C "$REPO_DIR/vm-files/home/agent" -cf - . \
    | ssh "${SSH_OPTS[@]}" "agent@$VM_HOST" 'tar -C /home/agent -xf - '

echo "==> Installing the forced command"
tar -C "$REPO_DIR/vm-files/usr/local/bin" -cf - agent-exec \
    | ssh "${SSH_OPTS[@]}" "agent@$VM_HOST" '
        set -eu
        tmp="$(mktemp -d)"
        tar -C "$tmp" -xf -
        sudo install -m 0755 -o root -g root "$tmp/agent-exec" /usr/local/bin/agent-exec
        rm -rf "$tmp"
    '

echo "==> Fixing permissions"
ssh "${SSH_OPTS[@]}" "agent@$VM_HOST" '
    set -e
    chmod 0755 /home/agent/.claude/hooks/approve.py
    mkdir -p /home/agent/work /home/agent/memory
    # sudo: cloud-init leaves a couple of root-owned markers (.provisioned) in
    # /home/agent, and a plain chown -R as the agent user fails on those.
    sudo chown -R agent:agent /home/agent
'

echo "==> Verifying the guest side"
ssh "${SSH_OPTS[@]}" "agent@$VM_HOST" '
    set -eu
    # ~/.local/bin is not on PATH for a non-login shell, which is exactly the
    # environment agent-exec runs in.
    export PATH="$HOME/.local/bin:$PATH"
    echo -n "  claude:           "; claude --version
    echo -n "  agent-exec:       "; test -x /usr/local/bin/agent-exec && echo installed || echo MISSING
    echo -n "  gate fail-closed: "
    printf "{\"tool_name\":\"Write\",\"tool_input\":{}}" \
        | AGENT_APPROVAL_URL= AGENT_RUN_TOKEN= python3 /home/agent/.claude/hooks/approve.py \
        | python3 -c "import json,sys; print(json.load(sys.stdin)[\"hookSpecificOutput\"][\"permissionDecision\"])"
'

echo
echo "Updated files are in place."
