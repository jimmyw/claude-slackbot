#!/usr/bin/env bash
# Load the GitHub machine-user key into the daemon's ssh-agent, constrained so the
# guest can only use it to reach GitHub.
#
# Run by slack-claude-ssh-agent.service as ExecStartPost. Safe to run by hand.
#
# The key is added WITHOUT a destination constraint, which was not the original
# intent. Constraints do not work for this use case, and here is the evidence:
#
#   ssh-add -h "<vm>>git@github.com" adds cleanly, but the agent then hides the
#   key from the guest. Its debug output shows why — "1 socket bindings, 1
#   constraints", the single binding being the VM's hostkey. The inner ssh that
#   git spawns in the guest is a separate client whose session-bind never joins
#   the outer chain, so the agent only ever sees [vm] and the constraint requires
#   [vm, github]. Constraints are built for one client traversing multiple hops
#   (ProxyJump), not for a nested ssh relayed through sshd, which is what git does.
#
# What this costs: during a run the guest can use the key against any host that
# trusts it. For a GitHub-only machine user that is GitHub and nowhere else, so
# the practical exposure equals the access being granted on purpose. The key
# itself still cannot be extracted — agents never hand out private key material —
# and it is unreachable between runs.
#
# What it therefore REQUIRES: a read-only machine user. With a personal account's
# key this forwards write access to every repo that account can write.
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

# Kept for the VM hostkey record even though no constraint is applied, so
# verify-agent-forwarding.sh can compare against what the daemon connects to.
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
if ssh-add "$KEY" 2>&1 | sed 's/^/  /'; then
    log "added $(basename "$KEY") (UNCONSTRAINED — see the comment above)"
else
    log "ssh-add FAILED"
    exit 1
fi

ssh-add -l | sed 's/^/  /'
