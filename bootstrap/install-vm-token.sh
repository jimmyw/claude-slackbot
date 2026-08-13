#!/usr/bin/env bash
# Install the Claude Code long-lived token into the agent VM.
#
#   ./bootstrap/install-vm-token.sh <vm-ip>
#
# Prompts with echo off and pipes the token straight into the guest. It never
# appears on screen, in shell history, in argv (so not in `ps`), in this repo, or
# in any file on the host — only inside the VM, mode 0600, agent-owned.
#
# Why a file rather than an env var: `claude setup-token` tells you to export
# CLAUDE_CODE_OAUTH_TOKEN, but agent-exec runs as an ssh forced command, which
# gets a non-interactive non-login shell and reads neither ~/.bashrc nor
# ~/.profile. agent-exec sources this file instead.
#
# To rotate: run `claude setup-token` again and re-run this script. The previous
# token stays valid until you revoke it in the Anthropic console.
set -euo pipefail

VM_HOST="${1:-}"
ADMIN_KEY="${ADMIN_KEY:-$HOME/.ssh/agent_vm_admin_ed25519}"
TOKEN_PATH=/home/agent/.config/claude-agent/token

if [[ -z "$VM_HOST" ]]; then
    echo "usage: $0 <vm-ip>" >&2
    exit 64
fi

if [[ ! -f "$ADMIN_KEY" ]]; then
    echo "ERROR: no admin key at $ADMIN_KEY" >&2
    exit 1
fi

SSH=(ssh -i "$ADMIN_KEY" -o IdentitiesOnly=yes -o BatchMode=yes
     -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "admin@$VM_HOST")

echo "Paste the token from \`claude setup-token\` (input is hidden):"
read -rs TOKEN
echo

if [[ -z "$TOKEN" ]]; then
    echo "ERROR: empty token" >&2
    exit 64
fi

if [[ "$TOKEN" != sk-ant-oat* ]]; then
    echo "WARNING: that does not look like a setup-token value (expected sk-ant-oat...)." >&2
    read -rp "Continue anyway? [y/N] " confirm
    [[ "$confirm" == [yY] ]] || exit 1
fi

# The token travels on stdin only. The destination path rides in an environment
# variable on the remote command line, so the secret is never an argv entry.
printf '%s' "$TOKEN" | "${SSH[@]}" "TOKEN_DEST='$TOKEN_PATH' bash -s" <<'REMOTE'
set -eu
umask 077
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
cat > "$tmp"
# Written by admin but owned by agent: agent-exec has to read it, while admin is
# the only identity holding sudo. The agent account has none.
sudo install -D -m 0600 -o agent -g agent "$tmp" "$TOKEN_DEST"
REMOTE

unset TOKEN

echo "Installed:"
"${SSH[@]}" "sudo stat -c '  %A %U:%G %s bytes  %n' '$TOKEN_PATH'"

echo
echo "Checking the CLI accepts it (one small API call):"
if "${SSH[@]}" "TOKEN_SRC='$TOKEN_PATH' bash -s" <<'REMOTE'
set -eu
export PATH="/home/agent/.local/bin:$PATH"
CLAUDE_CODE_OAUTH_TOKEN="$(sudo cat "$TOKEN_SRC")"
export CLAUDE_CODE_OAUTH_TOKEN
# HOME=/tmp so this probe never writes into the agent's real config.
HOME=/tmp claude -p 'Reply with exactly: AUTHOK' --output-format json < /dev/null \
  | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("  result:", (d.get("result") or "")[:60])
sys.exit(1 if d.get("is_error") else 0)
'
REMOTE
then
    echo
    echo "Token works. The VM can authenticate on its own now."
else
    echo
    echo "The CLI did not accept that token. Run \`claude setup-token\` again and retry." >&2
    exit 1
fi
