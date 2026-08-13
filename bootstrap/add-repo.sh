#!/usr/bin/env bash
# Clone a repo into the agent's workspace.
#
#   ./bootstrap/add-repo.sh <vm-ip> <git-url> [directory-name]
#
# Clones as the *agent* user, which is the point of this script. Cloning as admin
# (or over your own tailscale login) leaves the tree owned by that user, and since
# the agent has no sudo it then cannot write a single file — the symptom is the
# agent reporting permission errors on work it was just asked to do, with a
# perfectly healthy-looking clone on disk.
#
# The agent's cwd is /home/agent/work, so anything cloned here is what it sees.
set -euo pipefail

WITH_SUBMODULES=false
ARGS=()
for arg in "$@"; do
    case "$arg" in
        --submodules) WITH_SUBMODULES=true ;;
        *) ARGS+=("$arg") ;;
    esac
done
set -- "${ARGS[@]+"${ARGS[@]}"}"

VM_HOST="${1:-}"
GIT_URL="${2:-}"
DIR_NAME="${3:-}"
ADMIN_KEY="${ADMIN_KEY:-$HOME/.ssh/agent_vm_admin_ed25519}"
WORKDIR=/home/agent/work

if [[ -z "$VM_HOST" || -z "$GIT_URL" ]]; then
    echo "usage: $0 <vm-ip> <git-url> [directory-name] [--submodules]" >&2
    exit 64
fi

if [[ -z "$DIR_NAME" ]]; then
    DIR_NAME="$(basename "${GIT_URL%.git}")"
fi

SSH=(ssh -i "$ADMIN_KEY" -o IdentitiesOnly=yes -o BatchMode=yes
     -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "admin@$VM_HOST")

echo "==> Cloning $GIT_URL as the agent user"
set +e
"${SSH[@]}" "GIT_URL='$GIT_URL' DIR_NAME='$DIR_NAME' WORKDIR='$WORKDIR' \
    WITH_SUBMODULES='$WITH_SUBMODULES' bash -s" <<'REMOTE'
set -eu

# Identity of a clone is its REMOTE, not its directory name. Without this, giving
# a repo a short local name once means a later invocation without that third
# argument clones the same repo a second time under its default name — two working
# copies of one repo, diverging silently.
existing=$(
    for d in "$WORKDIR"/*/; do
        sudo test -d "$d.git" || continue
        # As agent, not root: git's safe.directory protection refuses a repo
        # owned by another user and this returns empty rather than failing, so a
        # root-run check silently finds no existing clone and happily makes a
        # second one.
        url=$(sudo -u agent -H git -C "$d" config --get remote.origin.url 2>/dev/null || true)
        [ -n "$url" ] || continue
        # Compare owner/repo only: the same repo may be addressed as
        # git@github.com:o/r.git, git@github.com-alias:o/r.git or an https URL.
        # Two expressions, not one: POSIX ERE has no lazy quantifier, so a `+?`
        # here silently leaves the .git suffix attached and nothing ever matches.
        norm=$(printf '%s' "$url"     | sed -E 's#\.git/?$##; s#^.*[:/]([^:/]+/[^:/]+)$#\1#')
        want=$(printf '%s' "$GIT_URL" | sed -E 's#\.git/?$##; s#^.*[:/]([^:/]+/[^:/]+)$#\1#')
        if [ "$norm" = "$want" ]; then printf '%s' "${d%/}"; break; fi
    done
)

if [ -n "$existing" ] && [ "$existing" != "$WORKDIR/$DIR_NAME" ]; then
    echo "    this repo is ALREADY cloned at $existing"
    echo "    (matched on remote, not directory name — refusing to make a second copy)"
    echo "    to move it:  sudo mv '$existing' '$WORKDIR/$DIR_NAME'"
    echo "    a plain mv is safe: git stores only relative paths internally."
    # 3 = "already present elsewhere". The local side must stop too: an `exit 0`
    # here only ends this remote shell, and the caller would then verify a
    # directory that was never created.
    exit 3
fi

# sudo test, not [ -e ]: admin cannot traverse /home/agent, so a bare test
# reports "does not exist" for a directory that plainly does — and the clone
# below then fails with "already exists and is not an empty directory".
if sudo test -e "$WORKDIR/$DIR_NAME"; then
    echo "    $WORKDIR/$DIR_NAME already exists; fetching instead of cloning"
    sudo -u agent -H git -C "$WORKDIR/$DIR_NAME" remote -v | sed "s/^/      /"
    sudo -u agent -H git -C "$WORKDIR/$DIR_NAME" fetch --all --prune --quiet
    exit 0
fi

# -H so git reads /home/agent/.gitconfig rather than admin's.
# --quiet: git writes progress with carriage returns, which turns a log into
# one unreadable line. Failures still go to stderr.
sudo -u agent -H git clone --quiet "$GIT_URL" "$WORKDIR/$DIR_NAME"
echo "    cloned $(sudo -u agent -H git -C "$WORKDIR/$DIR_NAME" rev-list --count HEAD) commits"

if [ "$WITH_SUBMODULES" = true ]; then
    echo "    initialising submodules"
    sudo -u agent -H git -C "$WORKDIR/$DIR_NAME" \
        submodule update --init --recursive --quiet \
      || echo "    SUBMODULES FAILED — each is a separate repo needing its own read access"
fi
REMOTE
clone_status=$?
set -e

if [[ "$clone_status" -eq 3 ]]; then
    exit 0        # already cloned elsewhere; the remote side explained where
elif [[ "$clone_status" -ne 0 ]]; then
    echo "clone failed (exit $clone_status)" >&2
    exit "$clone_status"
fi

echo
echo "==> Verifying the agent can actually work in it"
"${SSH[@]}" "DIR='$WORKDIR/$DIR_NAME' bash -s" <<'REMOTE'
set -eu
fail=0

# sudo: /home/agent is drwx------ agent:agent, so admin cannot stat inside it.
owner=$(sudo stat -c '%U:%G' "$DIR")
if [ "$owner" = "agent:agent" ]; then
    echo "  PASS  owned by agent:agent"
else
    echo "  FAIL  owned by $owner — the agent cannot write here"; fail=1
fi

# Ownership of the top directory is not the claim; writability is.
if sudo -u agent test -w "$DIR"; then
    echo "  PASS  writable by agent"
else
    echo "  FAIL  not writable by agent"; fail=1
fi

# A stray root-owned file inside the tree breaks exactly one file and is easy to
# miss, so check the whole tree rather than just the root.
foreign=$(sudo find "$DIR" -not -user agent -print -quit 2>/dev/null || true)
if [ -z "$foreign" ]; then
    echo "  PASS  no files in the tree owned by anyone else"
else
    echo "  FAIL  not owned by agent: $foreign"; fail=1
fi

if sudo test -f "$DIR/.gitmodules"; then
    total=$(sudo -u agent -H git -C "$DIR" config -f .gitmodules --get-regexp 'submodule\..*\.path' | wc -l)
    missing=0
    while read -r path; do
        [ -z "$path" ] && continue
        # An uninitialised submodule is an EMPTY directory, not an absent one, so
        # the agent sees a real-looking but contentless component and will happily
        # document it as such. Count them explicitly.
        if [ -z "$(sudo ls -A "$DIR/$path" 2>/dev/null)" ]; then
            missing=$((missing + 1))
        fi
    done <<< "$(sudo -u agent -H git -C "$DIR" config -f .gitmodules --get-regexp 'submodule\..*\.path' | awk '{print $2}')"

    if [ "$missing" -gt 0 ]; then
        echo "  WARN  $missing of $total submodules are EMPTY directories"
        echo "        The agent will read this tree as complete and document it"
        echo "        wrongly. Each submodule is a separate private repo and needs"
        echo "        its own read access; re-run with --submodules once that is in"
        echo "        place. Repos required:"
        sudo -u agent -H git -C "$DIR" config -f .gitmodules --get-regexp 'submodule\..*\.url' \
          | awk '{print "          " $2}' | sort -u
    else
        echo "  PASS  all $total submodules populated"
    fi
fi

if sudo -u agent -H git -C "$DIR" status --short >/dev/null 2>&1; then
    branch=$(sudo -u agent -H git -C "$DIR" rev-parse --abbrev-ref HEAD)
    echo "  PASS  git works as agent (on $branch)"
else
    echo "  FAIL  git does not work as the agent user"; fail=1
fi

exit "$fail"
REMOTE

echo
echo "Done. The agent's cwd is $WORKDIR, so in Slack you can refer to it as"
echo "  $DIR_NAME/...  or  $WORKDIR/$DIR_NAME/..."
