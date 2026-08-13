#!/usr/bin/env bash
# Verify the agent VM is correctly provisioned.
#
#   ./bootstrap/verify-guest.sh <vm-ip>
#
# Checks what a fresh provision is supposed to produce, in the environment it
# actually runs in. The PATH check matters: agent-exec runs as an ssh forced
# command, which gets a non-login shell whose PATH excludes ~/.local/bin — where
# the Claude Code installer puts the binary. A `claude --version` that works over
# an interactive login proves nothing about that.
set -euo pipefail

VM_HOST="${1:-}"
ADMIN_KEY="${ADMIN_KEY:-$HOME/.ssh/agent_vm_admin_ed25519}"
DAEMON_KEY="${DAEMON_KEY:-$HOME/.ssh/agent_vm_ed25519}"

if [[ -z "$VM_HOST" ]]; then
    echo "usage: $0 <vm-ip>" >&2
    exit 64
fi

COMMON=(-o IdentitiesOnly=yes -o BatchMode=yes
        -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
ADMIN_SSH=(ssh -i "$ADMIN_KEY" "${COMMON[@]}" "admin@$VM_HOST")
DAEMON_SSH=(ssh -i "$DAEMON_KEY" "${COMMON[@]}" "agent@$VM_HOST")

pass=0
fail=0
report() {
    if [[ "$2" == "ok" ]]; then
        printf '  PASS  %-44s %s\n' "$1" "${3:-}"; pass=$((pass + 1))
    else
        printf '  FAIL  %-44s %s\n' "$1" "${3:-}"; fail=$((fail + 1))
    fi
}

echo "[1] cloud-init finished"
status=$("${ADMIN_SSH[@]}" 'cloud-init status --format json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)[\"status\"])" 2>/dev/null || cloud-init status | awk "{print \$2}"' 2>/dev/null || echo unknown)
[[ "$status" == "done" ]] && report "cloud-init status" ok "$status" \
                          || report "cloud-init status" no "$status"

echo "[2] guest tooling"
for tool in git jq rg python3; do
    if "${ADMIN_SSH[@]}" "command -v $tool >/dev/null 2>&1"; then
        report "$tool installed" ok
    else
        report "$tool installed" no "missing"
    fi
done

echo "[3] Claude Code reachable in a NON-login shell"
# This is the environment agent-exec actually runs in.
if version=$("${ADMIN_SSH[@]}" 'sudo -u agent -H bash -c '"'"'export PATH=$HOME/.local/bin:$PATH; claude --version'"'"'' 2>/dev/null); then
    report "claude --version" ok "$version"
else
    report "claude --version" no "not found as user agent with ~/.local/bin on PATH"
fi

echo "[4] provisioned files — the gate must NOT be agent-writable"
file_checks=0
while read -r mode owner path; do
    case "$path" in
        /etc/claude-agent/*|/usr/local/bin/agent-exec|/home/agent/CLAUDE.md)
            file_checks=$((file_checks + 1))
            # The identity a control constrains must not be able to edit it.
            [[ "$owner" == "root:root" ]] \
                && report "$(basename "$path") root-owned" ok "$mode" \
                || report "$(basename "$path") root-owned" no "owned by $owner"
            ;;
        *MEMORY.md)
            file_checks=$((file_checks + 1))
            # The one thing the agent must be able to write.
            [[ "$owner" == "agent:agent" ]] \
                && report "MEMORY.md agent-writable" ok "$mode" \
                || report "MEMORY.md agent-writable" no "owned by $owner"
            ;;
    esac
done < <("${ADMIN_SSH[@]}" 'sudo stat -c "%A %U:%G %n" /usr/local/bin/agent-exec \
    /etc/claude-agent/approve.py /etc/claude-agent/settings.json \
    /home/agent/CLAUDE.md /home/agent/memory/MEMORY.md')

# A missing file must fail, not vanish. Five paths are stat'd above; anything less
# means one did not exist and its check silently never ran.
expected_files=5
[[ "$file_checks" -eq "$expected_files" ]] \
    && report "all $expected_files provisioned files present" ok \
    || report "provisioned file count" no "checked $file_checks of $expected_files"

# Ownership alone is not the claim; not-writable-by-agent is. Test it directly.
for target in /etc/claude-agent/settings.json /etc/claude-agent/approve.py \
              /usr/local/bin/agent-exec; do
    if "${ADMIN_SSH[@]}" "sudo -u agent test -w $target" 2>/dev/null; then
        report "agent cannot write $(basename "$target")" no "IT CAN"
    else
        report "agent cannot write $(basename "$target")" ok
    fi
done

echo "[4b] the agent account has no sudo"
if "${ADMIN_SSH[@]}" 'sudo -u agent sudo -n true' 2>/dev/null; then
    report "agent has no passwordless sudo" no "IT DOES"
else
    report "agent has no passwordless sudo" ok
fi

echo "[5] the approval hook fails closed"
decision=$("${ADMIN_SSH[@]}" 'printf "{\"tool_name\":\"Write\",\"tool_input\":{}}" | AGENT_APPROVAL_URL= AGENT_RUN_TOKEN= python3 /etc/claude-agent/approve.py | python3 -c "import json,sys; print(json.load(sys.stdin)[\"hookSpecificOutput\"][\"permissionDecision\"])"' 2>/dev/null || echo error)
[[ "$decision" == "deny" ]] && report "unwired gate denies" ok "$decision" \
                            || report "unwired gate denies" no "$decision"

decision=$("${ADMIN_SSH[@]}" 'printf "{\"tool_name\":\"Read\",\"tool_input\":{}}" | AGENT_APPROVAL_URL=x AGENT_RUN_TOKEN=y python3 /etc/claude-agent/approve.py | python3 -c "import json,sys; print(json.load(sys.stdin)[\"hookSpecificOutput\"][\"permissionDecision\"])"' 2>/dev/null || echo error)
[[ "$decision" == "allow" ]] && report "Read auto-allowed" ok "$decision" \
                            || report "Read auto-allowed" no "$decision"

echo "[6] the daemon key is confined to the forced command"
# A shell request must not get a shell. agent-exec rejects an empty job with 64,
# which is the healthy signal: ssh authenticated and the forced command ran.
out=$("${DAEMON_SSH[@]}" 'id' 2>&1 </dev/null || true)
if grep -q 'uid=' <<<"$out"; then
    report "daemon key cannot get a shell" no "got a shell!"
else
    report "daemon key cannot get a shell" ok
fi

set +e
"${DAEMON_SSH[@]}" </dev/null >/dev/null 2>&1
code=$?
set -e
[[ "$code" -eq 64 ]] && report "forced command runs (empty job -> 64)" ok \
                     || report "forced command exit code" no "$code"

echo
echo "passed=$pass failed=$fail"
if (( fail > 0 )); then
    echo "GUEST VERIFICATION FAILED" >&2
    exit 1
fi
echo "Guest verified."
