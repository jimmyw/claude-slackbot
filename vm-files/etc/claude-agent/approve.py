#!/usr/bin/env python3
"""PreToolUse approval gate.

Runs inside the VM, root-owned and outside the agent's home so the identity it
constrains cannot edit it. Holds no Slack credential: it POSTs the pending tool
call to the daemon over the SSH reverse tunnel and blocks on the daemon's verdict,
which the daemon only returns after the authorized operator clicks in Slack.

Three tiers of decision:

  1. read-only tools               -> allow, never ask
  2. writes inside the workspace   -> allow, never ask (reviewable as a git diff)
  3. everything else, Bash first   -> ask a human, and deny on timeout

Tier 2 exists because documentation work is dozens of writes and each one was a
separate button press. It is deliberately narrow: only the file-writing tools,
only for paths that resolve inside the workspace. Bash is never in tier 1 or 2 —
it is the universal escape hatch, and a gate that lets Bash through gates nothing.

The contract (verified against Claude Code 2.1.231):

  stdin   {"session_id", "transcript_path", "cwd", "prompt_id",
           "permission_mode", "effort", "hook_event_name",
           "tool_name", "tool_input", "tool_use_id"}

  stdout  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                  "permissionDecision": "allow" | "deny",
                                  "permissionDecisionReason": "..."}}

Every failure path denies. A hook that errors out also blocks the tool, so the
gate is fail-closed twice over — but we still emit an explicit deny so the model
gets a readable reason instead of a stack trace.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# Tier 1: read-only inspection, plus the agent's own todo bookkeeping.
#
# Bash is deliberately absent. A gate that blocks Write but allows Bash blocks
# nothing, because `bash -c 'echo > file'` does the same job. Observed in testing:
# denied a Write, the model immediately began trying Bash alternatives.
AUTO_ALLOW = frozenset(
    {
        "Read",
        "Grep",
        "Glob",
        "TodoWrite",
        "NotebookRead",
    }
)

# Tier 2: tools that write exactly one file, to a path we can check. Anything that
# can affect more than the named path (Bash, WebFetch, Task) must not be here.
WORKSPACE_WRITE_TOOLS = {
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path", "file_path"),
}

# The agent's workspace: repo clones and scratch files. Everything the agent is
# meant to produce lives here, and everything here is reviewable as a git diff.
WORKSPACE = "/home/agent/work"

# Must stay comfortably below the hook `timeout` in settings.json, so we return a
# real deny rather than being killed mid-wait by the harness.
REQUEST_TIMEOUT_S = 700


# --- Bash policy ------------------------------------------------------------
#
# Permissive by default: Bash runs without asking unless the command trips one of
# the rules below. The reasoning is that the VM is the security boundary, not this
# gate — egress is outbound-only with the LAN denied, there is no credential at
# rest, and this file is root-owned so the agent cannot edit it. Read/Grep are
# already unrestricted, so the agent can read every repo here regardless; what
# remains worth a human is anything that changes the machine, escalates, or
# reaches back out of the workspace.
#
# AGENT_POLICY selects the mode:
#   open        nothing is ever asked; the gate is effectively off
#   permissive  the default, described above
#   strict      every Bash call is asked about

# Commands that change the machine or escalate. Matched as whole words anywhere in
# the command, so `sh -c "sudo x"` is caught as well as a bare `sudo x`.
_ASK_COMMANDS = frozenset(
    {
        "sudo", "doas", "su", "visudo",
        "useradd", "usermod", "userdel", "groupadd", "passwd", "chpasswd",
        "systemctl", "service",
        "reboot", "shutdown", "poweroff", "halt", "init", "telinit",
        "mount", "umount", "mkfs", "fdisk", "parted", "mkswap", "swapon",
        "dd", "shred",
        "iptables", "nft", "ufw", "firewall-cmd",
        "insmod", "rmmod", "modprobe",
        "crontab", "at", "batch",
        "tailscale", "tailscaled",
        "apt", "apt-get", "dpkg", "yum", "dnf", "pacman", "snap",
    }
)

# Two-word forms that matter more than their first word.
_ASK_PHRASES = (
    "git push",
    "git remote add",
    "git remote set-url",
    "git config --global",
    "npm publish",
    "pip install",          # writes outside the workspace
)

# Commands that create, destroy, relocate or re-permission. Allowed inside the
# workspace; asked about whenever they name a path outside it.
#
# `cp` and `install` belong here for a specific reason: without them
# `cp /tmp/x /usr/local/bin/agent-exec` reads as harmless, and overwriting the
# forced command is about the most useful thing an agent could do to escape.
_MUTATORS = frozenset(
    {
        "rm", "rmdir", "unlink", "mv", "cp", "install", "tee", "touch", "mkdir",
        "chmod", "chown", "chgrp", "truncate", "ln", "patch", "gzip", "tar",
    }
)

_HOME = "/home/agent"

# Scratch and sinks: harmless to name whatever the command does with them.
_BENIGN_ANY = (
    "/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin", "/dev/zero",
    "/dev/urandom", "/tmp/", "/tmp",
)

# Harmless to READ or EXECUTE, but not to write. The split matters: with these on
# one list, `cp /tmp/x /usr/local/bin/agent-exec` looked benign, which would have
# let the agent replace the forced command that confines it.
_BENIGN_READ_ONLY = (
    "/usr/bin/", "/bin/", "/usr/local/bin/", "/usr/sbin/", "/sbin/",
    "/usr/share/", "/usr/lib/", "/usr/include/", "/lib/", "/lib64/",
    "/proc/", "/sys/", "/opt/",
)


def _paths_outside_workspace(command: str, mutating: bool) -> list[str]:
    """Paths named in the command that leave the workspace.

    Deliberately crude: it inspects tokens, not shell semantics. A false positive
    costs one approval click; a false negative would let something through, so the
    bias is towards asking.

    When the command mutates, the read-only allowlist is not applied — writing to
    /usr/local/bin is nothing like reading from it.
    """
    benign = _BENIGN_ANY if mutating else _BENIGN_ANY + _BENIGN_READ_ONLY
    outside = []
    for raw in command.split():
        token = raw.strip("\"'()`,;|&<>")
        if token.startswith("~"):
            token = _HOME + token[1:]
        if not token.startswith("/"):
            continue
        if token == WORKSPACE or token.startswith(WORKSPACE + "/"):
            continue
        if any(token == b or token.startswith(b) for b in benign):
            continue
        outside.append(token)
    return outside


def bash_reason_to_ask(command: str) -> str | None:
    """Why this command needs a human, or None to let it run."""
    lowered = command.lower()

    for phrase in _ASK_PHRASES:
        if phrase in lowered:
            return f"`{phrase}` needs approval"

    words = {w.strip("\"'()`,;|&<>") for w in lowered.split()}
    hit = words & _ASK_COMMANDS
    if hit:
        return f"`{sorted(hit)[0]}` changes the machine or escalates"

    # Touching the agent's own dotfiles is how a run would outlive itself: its
    # gitconfig carries core.sshCommand for the forwarded ssh-agent, and its
    # .claude directory holds the CLI token.
    for marker in (".ssh", ".gitconfig", ".claude", ".config/claude-agent",
                   "authorized_keys", "id_ed25519", "id_rsa", ".bashrc",
                   ".profile"):
        if marker in command:
            return f"touches `{marker}`"

    # Every word that starts a command in the pipeline, so a mutator hidden after
    # a `&&` or a pipe is still seen.
    heads = {
        segment.split()[0]
        for segment in command.replace("&&", ";").replace("||", ";")
        .replace("|", ";").replace("\n", ";").split(";")
        if segment.split()
    }
    # A redirection writes even when the command itself does not.
    mutating = bool(heads & _MUTATORS) or ">" in command

    outside = _paths_outside_workspace(command, mutating)
    if outside:
        if mutating:
            return f"would write outside the workspace: `{outside[0]}`"
        if any(
            p.startswith(("/etc", "/root", "/boot", "/var", "/home/"))
            for p in outside
        ):
            return f"reaches outside the workspace: `{outside[0]}`"

    return None


def decide(decision: str, reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    sys.stdout.flush()
    sys.exit(0)


def inside_workspace(path: str, workspace: str = WORKSPACE) -> bool:
    """True only if `path` genuinely resolves inside the workspace.

    realpath is the load-bearing part. It collapses `..` and follows symlinks, so
    neither `work/../../etc/passwd` nor a symlink planted at `work/escape ->
    /etc` can be used to write outside. Non-existent leaf files are fine: realpath
    resolves the components that do exist and leaves the rest literal.

    Compares against workspace + os.sep so that a sibling directory whose name
    merely starts with the workspace path — /home/agent/workshop — is not treated
    as being inside /home/agent/work.
    """
    if not path or not isinstance(path, str):
        return False

    real_workspace = os.path.realpath(workspace)
    # A relative path is ambiguous here: the hook cannot know how the CLI will
    # resolve it, so refuse to guess and let a human look.
    if not os.path.isabs(path):
        return False

    real = os.path.realpath(path)
    return real == real_workspace or real.startswith(real_workspace + os.sep)


def workspace_write_target(tool_name: str, tool_input: object) -> str | None:
    """The single path a tier-2 tool would write, or None if this isn't one."""
    keys = WORKSPACE_WRITE_TOOLS.get(tool_name)
    if keys is None or not isinstance(tool_input, dict):
        return None
    for key in keys:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        decide("deny", f"approval gate could not parse the hook payload: {exc}")

    tool_name = payload.get("tool_name") or "<unknown>"
    tool_input = payload.get("tool_input")

    policy = os.environ.get("AGENT_POLICY", "permissive").strip().lower()

    # `open` disables the gate entirely. What still protects the machine is not
    # this file but the operating system: the agent has no sudo, and this hook,
    # settings.json and agent-exec are root-owned, so even in this mode the agent
    # cannot escalate or disable its own gate. What `open` does newly permit is
    # `git push` with the forwarded ssh-agent, and edits to the agent's own
    # dotfiles — which is why it is a deliberate choice and not a default.
    if policy == "open":
        sys.stderr.write(f"approve: open policy, allowing {tool_name}\n")
        decide("allow", "open policy: approvals are disabled")

    if tool_name in AUTO_ALLOW:
        decide("allow", f"{tool_name} is on the read-only allowlist")

    # Permissive Bash: allowed unless a rule above says otherwise.
    if tool_name == "Bash" and policy != "strict":
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        if isinstance(command, str) and command.strip():
            reason = bash_reason_to_ask(command)
            if reason is None:
                decide("allow", "permissive policy: nothing in this command needs a human")
            # else fall through and ask, carrying the reason in the log below.
            sys.stderr.write(f"approve: asking about Bash because {reason}\n")

    target = workspace_write_target(tool_name, tool_input)
    if target is not None:
        if inside_workspace(target):
            decide("allow", f"{tool_name} writes inside {WORKSPACE}")
        # Fall through to ask. A write aimed outside the workspace is exactly the
        # case a human should see, so it is not denied outright.

    url = os.environ.get("AGENT_APPROVAL_URL")
    token = os.environ.get("AGENT_RUN_TOKEN")
    if not url or not token:
        decide(
            "deny",
            "approval gate is not wired up (AGENT_APPROVAL_URL / "
            "AGENT_RUN_TOKEN unset), so no human can approve this call",
        )

    body = json.dumps(
        {
            "run_token": token,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_use_id": payload.get("tool_use_id"),
            "session_id": payload.get("session_id"),
            "cwd": payload.get("cwd"),
        }
    ).encode()

    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            verdict = json.load(response)
    except urllib.error.HTTPError as exc:
        decide("deny", f"approval service rejected the request (HTTP {exc.code})")
    except urllib.error.URLError as exc:
        decide("deny", f"could not reach the approval service: {exc.reason}")
    except TimeoutError:
        decide("deny", "no answer from Slack before the approval window closed")
    except (json.JSONDecodeError, ValueError):
        decide("deny", "approval service returned a malformed verdict")

    approved = verdict.get("approved") is True
    reason = verdict.get("reason") or (
        "approved in Slack" if approved else "denied in Slack"
    )
    decide("allow" if approved else "deny", reason)


if __name__ == "__main__":
    main()
