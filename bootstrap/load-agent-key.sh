#!/usr/bin/env bash
# Load the GitHub machine-user key into the daemon's ssh-agent, constrained so the
# guest can only use it to reach GitHub.
#
# Run by slack-claude-ssh-agent.service as ExecStartPost. Safe to run by hand.
#
# The destination constraint is the reason this design is acceptable at all.
# Plain agent forwarding lets whoever controls the remote end use the key for
# anything they like; `ssh-add -h agent@vm>git@github.com` pins it to that single
# hop, so a compromised guest cannot authenticate to terra or anywhere else with
# it. Constraints need the hostkeys of every hop, hence -H.
set -euo pipefail

STATE_DIR="${STATE_DIR:-$HOME/.local/share/slack-claude}"
KEY="${MACHINE_USER_KEY:-$HOME/.ssh/github_machine_user_ed25519}"
KNOWN_HOSTS="$STATE_DIR/agent-constraint-known-hosts"
ENV_FILE="${ENV_FILE:-$HOME/slackbot/daemon/.env}"

export SSH_AUTH_SOCK="${SSH_AUTH_SOCK:-$STATE_DIR/ssh-agent.sock}"

log() { printf 'load-agent-key: %s\n' "$*"; }

# VM_HOST is the first hop in the constraint, so it has to match what the daemon
# actually connects to.
VM_HOST="${VM_HOST:-}"
if [[ -z "$VM_HOST" && -r "$ENV_FILE" ]]; then
    VM_HOST="$(grep -E '^VM_HOST=' "$ENV_FILE" | tail -1 | cut -d= -f2-)"
fi

if [[ -z "$VM_HOST" ]]; then
    log "VM_HOST not set and not found in $ENV_FILE — cannot build the constraint"
    exit 1
fi

if [[ ! -f "$KEY" ]]; then
    log "no machine-user key at $KEY — run bootstrap/50-machine-user-key.sh"
    exit 0   # not an error: the agent can run keyless until the key exists
fi

# Wait for ssh-agent to create its socket; ExecStartPost can win the race.
for _ in $(seq 1 50); do
    [[ -S "$SSH_AUTH_SOCK" ]] && break
    sleep 0.1
done
if [[ ! -S "$SSH_AUTH_SOCK" ]]; then
    log "ssh-agent socket never appeared at $SSH_AUTH_SOCK"
    exit 1
fi

mkdir -p "$STATE_DIR"

# Hostkeys for both hops. Without these ssh-add refuses the constraint, and
# without the *right* ones a MITM on either hop could harvest signatures.
: > "$KNOWN_HOSTS.new"
if ssh-keyscan -t rsa,ecdsa,ed25519 github.com 2>/dev/null >> "$KNOWN_HOSTS.new"; then
    log "scanned github.com hostkeys"
else
    log "could not scan github.com hostkeys"
fi
if ssh-keyscan -t rsa,ecdsa,ed25519 "$VM_HOST" 2>/dev/null >> "$KNOWN_HOSTS.new"; then
    log "scanned $VM_HOST hostkeys"
else
    log "could not scan $VM_HOST hostkeys"
fi
if [[ ! -s "$KNOWN_HOSTS.new" ]]; then
    log "no hostkeys collected — refusing to add an unconstrained key"
    rm -f "$KNOWN_HOSTS.new"
    exit 1
fi
mv "$KNOWN_HOSTS.new" "$KNOWN_HOSTS"
chmod 0600 "$KNOWN_HOSTS"

# Replace rather than accumulate: re-running must not leave a stale constraint
# pointing at an old VM address.
ssh-add -D >/dev/null 2>&1 || true

# No user on the "from" hop: ssh-add rejects it with "cannot specify user on
# 'from' host", because at that point in the chain the user is not something the
# agent can verify. The destination hop may and should name one.
CONSTRAINT="$VM_HOST>git@github.com"
if ssh-add -H "$KNOWN_HOSTS" -h "$CONSTRAINT" "$KEY" 2>&1 | sed 's/^/  /'; then
    log "added $(basename "$KEY") constrained to $CONSTRAINT"
else
    log "ssh-add FAILED for constraint $CONSTRAINT"
    exit 1
fi

ssh-add -l | sed 's/^/  /'
