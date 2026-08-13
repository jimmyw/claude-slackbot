#!/usr/bin/env bash
# Bring the agent's commits back to the host for review.
#
#   ./bootstrap/review-agent-work.sh <vm-ip> <repo-name>
#
# The agent commits locally in the VM and has no credential to push with. This
# adds its working copy as a git remote on the host clone and fetches, so you
# review with ordinary git tooling and push yourself if you agree.
#
# Why a git fetch rather than copying files back: it brings commits, not a
# smeared working tree, so `git log` and `git diff` tell you exactly what the
# agent did and nothing of yours is overwritten.
set -euo pipefail

MIRROR="${MIRROR:-$HOME/agent-repos}"
WORKDIR=/home/agent/work
ADMIN_KEY="${ADMIN_KEY:-$HOME/.ssh/agent_vm_admin_ed25519}"

VM_HOST="${1:-}"
REPO_NAME="${2:-}"

if [[ -z "$VM_HOST" || -z "$REPO_NAME" ]]; then
    echo "usage: $0 <vm-ip> <repo-name>" >&2
    exit 64
fi

LOCAL="$MIRROR/$REPO_NAME"
if [[ ! -d "$LOCAL/.git" ]]; then
    echo "ERROR: no host clone at $LOCAL — run 40-sync-repos.sh first" >&2
    exit 1
fi

# git needs to read the guest copy over ssh. /home/agent is 0711 and work/ is
# 0755, so admin can reach it without sudo — which matters because git-upload-pack
# runs as the connecting user.
export GIT_SSH_COMMAND="ssh -F /dev/null -i $ADMIN_KEY -o IdentitiesOnly=yes \
-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"

REMOTE_URL="ssh://admin@$VM_HOST$WORKDIR/$REPO_NAME"

if git -C "$LOCAL" remote get-url agent-vm >/dev/null 2>&1; then
    git -C "$LOCAL" remote set-url agent-vm "$REMOTE_URL"
else
    git -C "$LOCAL" remote add agent-vm "$REMOTE_URL"
fi

echo "==> Fetching from the agent's copy"
if ! git -C "$LOCAL" fetch --quiet agent-vm '+refs/heads/*:refs/remotes/agent-vm/*'; then
    echo "ERROR: fetch failed. If it says 'not a git repository', the agent's copy" >&2
    echo "may not exist yet — run 40-sync-repos.sh $VM_HOST $REPO_NAME first." >&2
    exit 1
fi

echo
echo "==> What the agent changed"
current=$(git -C "$LOCAL" rev-parse --abbrev-ref HEAD)
for ref in $(git -C "$LOCAL" for-each-ref --format='%(refname:short)' refs/remotes/agent-vm/); do
    branch="${ref#agent-vm/}"
    base="origin/$branch"
    git -C "$LOCAL" rev-parse --verify --quiet "$base" >/dev/null || base="$current"
    ahead=$(git -C "$LOCAL" rev-list --count "$base..$ref" 2>/dev/null || echo 0)
    if [[ "$ahead" -gt 0 ]]; then
        echo "  $ref: $ahead commit(s) beyond $base"
        git -C "$LOCAL" log --oneline "$base..$ref" | sed 's/^/      /'
        git -C "$LOCAL" diff --stat "$base..$ref" | tail -12 | sed 's/^/      /'
    else
        echo "  $ref: nothing new"
    fi
done

echo
echo "Review the full diff:"
echo "  git -C $LOCAL diff origin/main..agent-vm/main"
echo "Take it onto a branch and push it yourself (the agent cannot):"
echo "  git -C $LOCAL checkout -b docs-from-agent agent-vm/main"
echo "  git -C $LOCAL push -u origin docs-from-agent"

# Uncommitted work in the guest never reaches a fetch, and silently losing the
# agent's output would be worse than a noisy warning.
dirty=$(ssh -F /dev/null -i "$ADMIN_KEY" -o IdentitiesOnly=yes -o BatchMode=yes \
    -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 "admin@$VM_HOST" \
    "sudo -u agent -H git -C '$WORKDIR/$REPO_NAME' status --porcelain 2>/dev/null | wc -l" 2>/dev/null || echo 0)
if [[ "${dirty:-0}" -gt 0 ]]; then
    echo
    echo "NOTE: $dirty uncommitted file(s) in the agent's copy are NOT in this fetch."
    echo "      Ask it to commit, or inspect them directly:"
    echo "        ssh -i $ADMIN_KEY admin@$VM_HOST \\"
    echo "          \"sudo -u agent -H git -C $WORKDIR/$REPO_NAME status\""
fi
