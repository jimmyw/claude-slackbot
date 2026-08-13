#!/usr/bin/env bash
# Install the Claude Code long-lived token into the agent VM.
#
#   ./bootstrap/install-vm-token.sh <vm-ip>
#
# Prompts for the token with echo off and pipes it straight to the guest. The
# token never appears on your screen, in shell history, in this repo, or in any
# file on the host — it is written only inside the VM, mode 0600, agent-owned.
#
# Why a file and not an env var: `claude setup-token` tells you to export
# CLAUDE_CODE_OAUTH_TOKEN, but agent-exec runs as an ssh forced command, which
# gets a non-interactive non-login shell and reads neither ~/.bashrc nor
# ~/.profile. agent-exec sources this file instead.
#
# To rotate: run `claude setup-token` in the guest again and re-run this script.
set -euo pipefail

VM_HOST="${1:-}"
ADMIN_KEY="${ADMIN_KEY:-$HOME/.ssh/agent_vm_admin_ed25519}"
TOKEN_PATH="/home/agent/.config/claude-agent/token"

if [[ -z "$VM_HOST" ]]; then
    echo "usage: $0 <vm-ip>" >&2
    exit 64
fi

if [[ ! -f "$ADMIN_KEY" ]]; then
    echo "ERROR: no admin key at $ADMIN_KEY" >&2
    exit 1
fi

SSH=(ssh -i "$ADMIN_KEY" -o IdentitiesOnly=yes -o BatchMode=yes
     -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "agent@$VM_HOST")

echo "Paste the token from \`claude setup-token\` (input is hidden):"
read -rs TOKEN
echo

if [[ -z "$TOKEN" ]]; then
    echo "ERROR: empty token" >&2
    exit 64
fi

if [[ "$TOKEN" != sk-ant-oat* ]]; then
    echo "WARNING: that does not look like a setup-token value (expected sk-ant-oat…)." >&2
    read -rp "Continue anyway? [y/N] " confirm
    [[ "$confirm" == [yY] ]] || exit 1
fi

# umask before the write so the file is never briefly world-readable, and
# `cat >` from stdin so the token is never an argv entry visible in ps.
printf '%s' "$TOKEN" | "${SSH[@]}" '
    set -eu
    umask 077
    mkdir -p "$(dirname '"$TOKEN_PATH"')"
    cat > '"$TOKEN_PATH"'
    chmod 0600 '"$TOKEN_PATH"'
'
unset TOKEN

echo "Installed. Verifying:"
"${SSH[@]}" "stat -c '  %A %U:%G %s bytes  %n' $TOKEN_PATH"

echo
echo "Checking the CLI accepts it (this makes one small API call):"
if "${SSH[@]}" '
    export PATH="$HOME/.local/bin:$PATH"
    export CLAUDE_CODE_OAUTH_TOKEN="$(< '"$TOKEN_PATH"')"
    claude -p "Reply with exactly: AUTHOK" --output-format json < /dev/null 2>&1 \
      | python3 -c "import json,sys; d=json.load(sys.stdin); print(\"  \", d.get(\"result\",\"\")[:60]); sys.exit(0 if not d.get(\"is_error\") else 1)"
'; then
    echo
    echo "Token works. The VM can now authenticate on its own."
else
    echo
    echo "The CLI did not accept that token. Re-run \`claude setup-token\` in the" >&2
    echo "guest and try again." >&2
    exit 1
fi
