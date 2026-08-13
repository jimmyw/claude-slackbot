#!/usr/bin/env bash
# Verify the forwarded-agent setup.
#
#   ./bootstrap/verify-agent-forwarding.sh <vm-ip> [expected-github-user]
#
#   EXPECTED_GITHUB_USER=tibber-agent-ro ./bootstrap/verify-agent-forwarding.sh <ip>
#
# The key is forwarded UNCONSTRAINED. Destination constraints do not work for this
# use case — see the long comment in load-agent-key.sh for the evidence — so the
# security of the arrangement rests entirely on the key belonging to a read-only
# machine user. This script therefore treats "which GitHub account is this?" as a
# first-class check rather than a detail.
#
# What is still guaranteed, and checked below: the private key cannot be extracted
# from the guest, no key file exists there, and the agent is unreachable between
# runs.
set -euo pipefail

VM_HOST="${1:-}"
EXPECTED_USER="${2:-${EXPECTED_GITHUB_USER:-}}"
ADMIN_KEY="${ADMIN_KEY:-$HOME/.ssh/agent_vm_admin_ed25519}"
DAEMON_KEY="${DAEMON_KEY:-$HOME/.ssh/agent_vm_ed25519}"
STATE_DIR="$HOME/.local/share/slack-claude"
export SSH_AUTH_SOCK="$STATE_DIR/ssh-agent.sock"

if [[ -z "$VM_HOST" ]]; then
    echo "usage: $0 <vm-ip> [expected-github-user]" >&2
    exit 64
fi

pass=0; fail=0; warn=0
report() {
    case "$2" in
        ok)   printf '  PASS  %-44s %s\n' "$1" "${3:-}"; pass=$((pass+1)) ;;
        warn) printf '  WARN  %-44s %s\n' "$1" "${3:-}"; warn=$((warn+1)) ;;
        *)    printf '  FAIL  %-44s %s\n' "$1" "${3:-}"; fail=$((fail+1)) ;;
    esac
}

ADMIN_FWD=(ssh -F /dev/null -A -i "$ADMIN_KEY" -o IdentitiesOnly=yes
           -o BatchMode=yes -o StrictHostKeyChecking=accept-new
           -o ConnectTimeout=15 "admin@$VM_HOST")

echo "[1] host ssh-agent"
[[ -S "$SSH_AUTH_SOCK" ]] \
    && report "agent socket exists" ok "$SSH_AUTH_SOCK" \
    || report "agent socket exists" no "missing $SSH_AUTH_SOCK"

if keys=$(ssh-add -l 2>&1) && [[ "$keys" != *"no identities"* ]]; then
    report "a key is loaded" ok "$(printf '%s' "$keys" | head -1 | cut -c1-44)"
else
    report "a key is loaded" no "$keys"
fi

echo "[2] the guest can reach GitHub through the forwarded agent"
github=$("${ADMIN_FWD[@]}" 'ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -T git@github.com 2>&1 | head -1' 2>&1 || true)
if [[ "$github" == *"successfully authenticated"* ]]; then
    report "guest authenticates to GitHub" ok
else
    # An empty identity list here means forwarding is not reaching the guest — it
    # is a failure, not a security win. Scoring it as a pass hid a broken setup
    # once already.
    report "guest authenticates to GitHub" no "$github"
fi

echo "[3] WHICH GitHub account? (the whole security model rests on this)"
account=$(printf '%s' "$github" | sed -n 's/^Hi \([^!]*\)!.*/\1/p')
if [[ -z "$account" ]]; then
    report "identified the account" no "could not parse: ${github:0:44}"
elif [[ -z "$EXPECTED_USER" ]]; then
    report "account is a read-only machine user" warn "authenticates as '$account' — unverified"
    echo "        Pass the expected machine-user name to assert this:"
    echo "          $0 $VM_HOST <machine-user>"
elif [[ "$account" == "$EXPECTED_USER" ]]; then
    report "account is the expected machine user" ok "$account"
else
    report "account is the expected machine user" no "got '$account', want '$EXPECTED_USER'"
    echo "        An unconstrained forwarded key grants the guest everything that"
    echo "        account can do at GitHub, including push. A personal account here"
    echo "        means write access to every repo you can write."
fi

echo "[4] what remains guaranteed"
# Agents never expose private key material; confirm the guest sees at most public.
listing=$("${ADMIN_FWD[@]}" 'ssh-add -L 2>&1 | head -1' 2>&1 || true)
case "$listing" in
    *PRIVATE*|*BEGIN*) report "guest cannot obtain private key material" no "$listing" ;;
    *) report "guest cannot obtain private key material" ok "public/none only" ;;
esac

found=$("${ADMIN_FWD[@]}" 'sudo find /home /etc /root -name "*machine_user*" -o -name "id_ed25519" 2>/dev/null | head -3' 2>&1 || true)
[[ -z "$found" ]] \
    && report "no machine-user key file in the guest" ok \
    || report "no machine-user key file in the guest" no "$found"

# Without -A the guest must have no agent at all, which is what makes the
# credential unavailable between runs.
noagent=$(ssh -F /dev/null -i "$ADMIN_KEY" -o IdentitiesOnly=yes -o BatchMode=yes \
    -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "admin@$VM_HOST" \
    'ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -T git@github.com 2>&1 | head -1' 2>&1 || true)
if [[ "$noagent" == *"successfully authenticated"* ]]; then
    report "no GitHub access without forwarding" no "IT WORKED WITHOUT -A: $noagent"
else
    report "no GitHub access without forwarding" ok "denied when not forwarded"
fi

echo "[5] the daemon key still cannot get a shell"
out=$(ssh -F /dev/null -i "$DAEMON_KEY" -o IdentitiesOnly=yes -o BatchMode=yes \
    -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "agent@$VM_HOST" id 2>&1 </dev/null || true)
grep -q 'uid=' <<<"$out" \
    && report "daemon key cannot get a shell" no "got a shell!" \
    || report "daemon key cannot get a shell" ok

echo
echo "passed=$pass warnings=$warn failed=$fail"
if (( fail > 0 )); then
    echo "AGENT FORWARDING CHECK FAILED" >&2
    exit 1
fi
if (( warn > 0 )); then
    echo "Working, but the GitHub account is unverified — see the WARN above."
    exit 0
fi
echo "Forwarding verified."
