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

# tar rather than rsync: rsync is not installed in the guest and adding it just
# for this would be a package the agent never otherwise needs. Everything is
# unpacked to a staging dir first, then installed with explicit ownership — the
# gate's files must land root-owned, not owned by the identity they constrain.
echo "==> Shipping vm-files to admin@$VM_HOST"
tar -C "$REPO_DIR/vm-files" -cf - etc home usr \
    | ssh "${SSH_OPTS[@]}" "admin@$VM_HOST" '
        set -eu
        rm -rf /tmp/vmfiles && mkdir -p /tmp/vmfiles
        tar -C /tmp/vmfiles -xf -

        # Root-owned: the agent must not be able to edit its own gate.
        sudo install -D -m 0755 -o root -g root \
            /tmp/vmfiles/etc/claude-agent/approve.py /etc/claude-agent/approve.py
        sudo install -D -m 0644 -o root -g root \
            /tmp/vmfiles/etc/claude-agent/settings.json /etc/claude-agent/settings.json
        sudo install -D -m 0644 -o root -g root \
            /tmp/vmfiles/home/agent/CLAUDE.md /home/agent/CLAUDE.md
        sudo install -D -m 0755 -o root -g root \
            /tmp/vmfiles/usr/local/bin/agent-exec /usr/local/bin/agent-exec

        # The one thing the agent needs to write. Never clobber an existing
        # MEMORY.md — it is the only state that survives between sessions.
        if [ ! -f /home/agent/memory/MEMORY.md ]; then
            sudo install -D -m 0644 -o agent -g agent \
                /tmp/vmfiles/home/agent/memory/MEMORY.md /home/agent/memory/MEMORY.md
        else
            echo "    keeping existing memory/MEMORY.md"
        fi

        rm -rf /tmp/vmfiles
    '

echo "==> Verifying the guest side"
ssh "${SSH_OPTS[@]}" "admin@$VM_HOST" '
    set -eu

    # As agent, not admin: /home/agent is drwx------ agent:agent, so admin cannot
    # even see the CLI binary and this reports "command not found" for a healthy
    # install. ~/.local/bin is also absent from a non-login PATH, which is exactly
    # the environment agent-exec runs in.
    echo -n "  claude:              "
    sudo -u agent -H bash -c "export PATH=\$HOME/.local/bin:\$PATH; claude --version"

    echo -n "  agent-exec:          "
    test -x /usr/local/bin/agent-exec && echo installed || echo MISSING

    gate() { # tool_name json_input -> allow|deny
        printf "{\"tool_name\":\"%s\",\"tool_input\":%s}" "$1" "$2" \
          | AGENT_APPROVAL_URL= AGENT_RUN_TOKEN= python3 /etc/claude-agent/approve.py \
          | python3 -c "import json,sys; print(json.load(sys.stdin)[\"hookSpecificOutput\"][\"permissionDecision\"])"
    }

    # With no tunnel wired up, anything that needs a human must fail closed, and
    # anything auto-allowed must still come back allow.
    echo -n "  Read:                "; gate Read "{}"
    echo -n "  Write in workspace:  "; gate Write "{\"file_path\":\"/home/agent/work/x.md\"}"
    echo -n "  Write outside:       "; gate Write "{\"file_path\":\"/etc/passwd\"}"
    echo -n "  Write via traversal: "; gate Write "{\"file_path\":\"/home/agent/work/../../etc/passwd\"}"
    echo -n "  Bash in workspace:   "; gate Bash "{\"command\":\"ls /home/agent/work\"}"
'

echo
echo "Updated files are in place."
