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

VM_HOST="${1:-}"
GIT_URL="${2:-}"
DIR_NAME="${3:-}"
ADMIN_KEY="${ADMIN_KEY:-$HOME/.ssh/agent_vm_admin_ed25519}"
WORKDIR=/home/agent/work

if [[ -z "$VM_HOST" || -z "$GIT_URL" ]]; then
    echo "usage: $0 <vm-ip> <git-url> [directory-name]" >&2
    exit 64
fi

if [[ -z "$DIR_NAME" ]]; then
    DIR_NAME="$(basename "${GIT_URL%.git}")"
fi

SSH=(ssh -i "$ADMIN_KEY" -o IdentitiesOnly=yes -o BatchMode=yes
     -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "admin@$VM_HOST")

echo "==> Cloning $GIT_URL as the agent user"
"${SSH[@]}" "GIT_URL='$GIT_URL' DIR_NAME='$DIR_NAME' WORKDIR='$WORKDIR' bash -s" <<'REMOTE'
set -eu

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
REMOTE

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
