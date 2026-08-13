#!/usr/bin/env bash
# Clone/update repos on the HOST and sync working copies into the agent's VM.
#
#   ./bootstrap/40-sync-repos.sh <vm-ip> tibber/foo tibber/bar
#   ./bootstrap/40-sync-repos.sh <vm-ip> -f repos.txt
#   ./bootstrap/40-sync-repos.sh <vm-ip> --all          # every repo you can read
#
# The GitHub credential lives ONLY on terra. The VM never holds one, so a
# compromised agent gets source it already had rather than read access to the org.
# That is the whole point of doing it this way instead of putting a token in the
# guest, and it is why the agent cannot fetch on its own — you run this.
#
# Submodules work here without any per-repo key or URL rewriting, because the host
# credential covers every repo the submodules point at. That is what made deploy
# keys unworkable: one key per repo, and submodule URLs are plain github.com.
#
# Changes the agent makes come back the other way, see review-agent-work.sh.
set -euo pipefail

MIRROR="${MIRROR:-$HOME/agent-repos}"
WORKDIR=/home/agent/work
ADMIN_KEY="${ADMIN_KEY:-$HOME/.ssh/agent_vm_admin_ed25519}"
DEFAULT_OWNER="${DEFAULT_OWNER:-tibber}"

VM_HOST="${1:-}"
shift || true

if [[ -z "$VM_HOST" || $# -eq 0 ]]; then
    echo "usage: $0 <vm-ip> <owner/repo>... | -f <list-file> | --all" >&2
    exit 64
fi

# --- collect the repo list -------------------------------------------------
REPOS=()
case "${1:-}" in
    -f)
        [[ -n "${2:-}" && -f "${2:-}" ]] || { echo "ERROR: list file not found: ${2:-}" >&2; exit 64; }
        while read -r line; do
            line="${line%%#*}"                     # strip comments
            line="$(printf '%s' "$line" | tr -d '[:space:]')"
            [[ -n "$line" ]] && REPOS+=("$line")
        done < "$2"
        ;;
    --all)
        echo "==> Enumerating repos you can read in '$DEFAULT_OWNER'"
        mapfile -t REPOS < <(gh repo list "$DEFAULT_OWNER" --limit 500 \
            --no-archived --json nameWithOwner --jq '.[].nameWithOwner')
        echo "    found ${#REPOS[@]}"
        ;;
    *)
        REPOS=("$@")
        ;;
esac

# Bare names get the default owner, so a list file can just say "foo".
for i in "${!REPOS[@]}"; do
    [[ "${REPOS[$i]}" == */* ]] || REPOS[$i]="$DEFAULT_OWNER/${REPOS[$i]}"
done

if (( ${#REPOS[@]} == 0 )); then
    echo "ERROR: no repos to sync" >&2
    exit 64
fi

# --- preflight -------------------------------------------------------------
if ! gh auth status >/dev/null 2>&1; then
    echo "ERROR: gh is not authenticated on this host." >&2
    echo "Run:  gh auth login          (choose SSH or HTTPS; either works)" >&2
    exit 1
fi

if [[ ! -f "$ADMIN_KEY" ]]; then
    echo "ERROR: no admin key at $ADMIN_KEY" >&2
    exit 1
fi

SSH_CMD="ssh -F /dev/null -i $ADMIN_KEY -o IdentitiesOnly=yes -o BatchMode=yes \
-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"

mkdir -p "$MIRROR"

# Let git use gh's credential for HTTPS so submodules resolve without per-repo
# keys. gh writes a credential helper into the user's git config on login; this
# just makes sure ssh-style submodule URLs go through it too.
export GIT_TERMINAL_PROMPT=0

echo "==> Syncing ${#REPOS[@]} repos via $MIRROR"
failed=()
synced=0

for slug in "${REPOS[@]}"; do
    name="${slug##*/}"
    dest="$MIRROR/$name"
    printf '  %-44s ' "$slug"

    if [[ -d "$dest/.git" ]]; then
        if git -C "$dest" fetch --quiet --prune --recurse-submodules=on-demand 2>/dev/null \
           && git -C "$dest" submodule update --init --recursive --quiet 2>/dev/null; then
            printf 'fetched'
        else
            echo "FETCH FAILED"; failed+=("$slug"); continue
        fi
    else
        if gh repo clone "$slug" "$dest" -- --quiet --recurse-submodules 2>/dev/null; then
            printf 'cloned'
        else
            echo "CLONE FAILED"; failed+=("$slug"); continue
        fi
    fi

    # Push the working copy in, agent-owned. --delete so a file removed upstream
    # disappears in the guest too; without it the agent reads a stale tree.
    # .git IS included: the agent commits locally and review-agent-work.sh fetches
    # those commits back. It has no credential, so it cannot push.
    if rsync -a --delete --quiet \
        -e "$SSH_CMD" \
        --rsync-path="sudo -u agent rsync" \
        "$dest/" "admin@$VM_HOST:$WORKDIR/$name/" 2>/dev/null; then
        echo " -> synced"
        synced=$((synced + 1))
    else
        echo " -> RSYNC FAILED"
        failed+=("$slug")
    fi
done

echo
echo "synced=$synced failed=${#failed[@]}"
if (( ${#failed[@]} )); then
    printf '  failed: %s\n' "${failed[*]}" >&2
    exit 1
fi
echo "The agent's workspace is $WORKDIR; refer to repos by name in Slack."
