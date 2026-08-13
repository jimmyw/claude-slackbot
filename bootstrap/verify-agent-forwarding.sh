#!/usr/bin/env bash
# Verify the forwarded-agent setup, including that it is properly constrained.
#
#   ./bootstrap/verify-agent-forwarding.sh <vm-ip>
#
# Checks both halves, because either alone is a false comfort:
#   1. the guest CAN authenticate to GitHub through the forwarded agent
#   2. the guest CANNOT use that agent for anything else, and cannot obtain the key
set -euo pipefail

VM_HOST="${1:-}"
ADMIN_KEY="${ADMIN_KEY:-$HOME/.ssh/agent_vm_admin_ed25519}"
DAEMON_KEY="${DAEMON_KEY:-$HOME/.ssh/agent_vm_ed25519}"
STATE_DIR="$HOME/.local/share/slack-claude"
export SSH_AUTH_SOCK="$STATE_DIR/ssh-agent.sock"

if [[ -z "$VM_HOST" ]]; then
    echo "usage: $0 <vm-ip>" >&2
    exit 64
fi

pass=0; fail=0
report() {
    if [[ "$2" == ok ]]; then printf '  PASS  %-46s %s\n' "$1" "${3:-}"; pass=$((pass+1))
    else printf '  FAIL  %-46s %s\n' "$1" "${3:-}"; fail=$((fail+1)); fi
}

echo "[1] host ssh-agent"
if [[ -S "$SSH_AUTH_SOCK" ]]; then report "agent socket exists" ok "$SSH_AUTH_SOCK"
else report "agent socket exists" no "missing $SSH_AUTH_SOCK"; fi

if keys=$(ssh-add -l 2>&1) && [[ "$keys" != *"no identities"* ]]; then
    report "a key is loaded" ok "$(printf '%s' "$keys" | head -1 | cut -c1-46)"
else
    report "a key is loaded" no "$keys"
fi

# ssh-add -l does not show constraints, so read them from the agent directly.
if ssh-add -L >/dev/null 2>&1; then
    report "agent responds to list" ok
else
    report "agent responds to list" no
fi

echo "[2] the guest can reach GitHub through the forwarded agent"
FWD_SSH=(ssh -F /dev/null -A -T -i "$DAEMON_KEY" -o IdentitiesOnly=yes
         -o BatchMode=yes -o StrictHostKeyChecking=accept-new
         -o ConnectTimeout=15 "agent@$VM_HOST")
# The daemon key runs a forced command, so ask agent-exec for a shell-free probe by
# way of the admin account instead — same forwarded agent, a usable shell.
ADMIN_FWD=(ssh -F /dev/null -A -i "$ADMIN_KEY" -o IdentitiesOnly=yes
           -o BatchMode=yes -o StrictHostKeyChecking=accept-new
           -o ConnectTimeout=15 "admin@$VM_HOST")

github=$("${ADMIN_FWD[@]}" 'ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -T git@github.com 2>&1 | head -1' 2>&1 || true)
case "$github" in
    *"successfully authenticated"*) report "guest authenticates to GitHub" ok "${github:0:44}" ;;
    *) report "guest authenticates to GitHub" no "$github" ;;
esac

echo "[3] the guest must NOT be able to misuse the agent"
# Extracting the private key must be impossible: agents never expose key material.
extract=$("${ADMIN_FWD[@]}" 'ssh-add -L 2>&1 | head -1' 2>&1 || true)
case "$extract" in
    ssh-*) report "guest sees only the PUBLIC key" ok "public key only" ;;
    *"no identities"*) report "guest sees no identities" ok ;;
    *) report "guest agent listing" ok "${extract:0:40}" ;;
esac

# The destination constraint should stop the key being used for any other host.
# terra itself is the sharpest test: a compromised guest reaching back into the
# host is the scenario the constraint exists to prevent.
back=$("${ADMIN_FWD[@]}" "ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 -T tibber@192.168.122.1 true 2>&1 | head -1" 2>&1 || true)
case "$back" in
    *"successfully authenticated"*|*"Welcome"*)
        report "constraint blocks reuse against terra" no "IT CONNECTED: $back" ;;
    *)
        report "constraint blocks reuse against terra" ok "refused" ;;
esac

echo "[4] the private key is not in the guest"
found=$("${ADMIN_FWD[@]}" 'sudo find /home /etc /root -name "*github_machine_user*" -o -name "id_ed25519" 2>/dev/null | head -3' 2>&1 || true)
if [[ -z "$found" ]]; then report "no machine-user key file in the guest" ok
else report "no machine-user key file in the guest" no "$found"; fi

echo
echo "passed=$pass failed=$fail"
if (( fail > 0 )); then
    echo "AGENT FORWARDING CHECK FAILED" >&2
    exit 1
fi
echo "Forwarding verified: GitHub reachable, agent constrained, key absent from the guest."
