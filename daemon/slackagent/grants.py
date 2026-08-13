"""Persistent "always allow" grants, and the matching that makes them safe.

A grant is (tool_name, pattern, match_type). Grants live in the daemon's sqlite on
the HOST, so the agent cannot grant itself anything — it can only ask, and the
answer is computed out here.

Three match types, because one shape does not fit the real traffic:

  any     the tool name is the whole scope. For tools with no field to scope by:
          ToolSearch, mcp__server__tool, TodoWrite.

  prefix  a command and optional subcommand: `git status` covers
          `git status --short`. The workhorse.

  exact   the complete command string, byte for byte. This is what makes commands
          containing redirection or command substitution grantable at all: there
          is no generalisation, so nothing can be appended to widen it.

Compound commands are handled by segmenting: `cd x && npm test` is covered when
BOTH `cd` and `npm test` are granted. Splitting is quote-aware, and every segment
must be covered independently, so a mis-split can only ever deny.

Two things are never generalised, whatever the operator clicks:

  * commands containing command substitution, redirection, a newline or a
    backslash. They can be granted `exact` but never `prefix`, because their
    effect is not determined by their opening words.
  * interpreters. Granting `prefix` on `sh`, `python3` or `xargs` is granting
    arbitrary code by another name, so those are exact-only too.
"""
from __future__ import annotations

from dataclasses import dataclass

ANY = "*"

MATCH_ANY = "any"
MATCH_PREFIX = "prefix"
MATCH_EXACT = "exact"

# Present in a command => it may only ever be granted `exact`. Segmenting cannot
# make these safe: substitution and redirection change what a command does without
# changing how it begins.
#
#   `  $(     command substitution
#   >  <      redirection, including process substitution <( >(
#   newline   a second command on the next line
#   \         escaping and line continuation, used to obfuscate the above
_NO_PREFIX_SEQUENCES = ("`", "$(", ">", "<", "\n", "\r", "\\")

# Shell operators that separate one command from the next. Split on the longest
# first so && is not seen as two &.
_OPERATORS = ("&&", "||", ";", "|", "&")

# Commands whose effect is destructive or system-wide. A prefix grant on one of
# these is never offered: `git status; rm -rf /home/agent` would otherwise put
# "Always allow: git status, rm" in front of the operator, and one careless click
# would auto-approve every future `rm`. Such a command can still be granted, but
# only as itself.
_DESTRUCTIVE = frozenset(
    {
        "rm", "rmdir", "unlink", "shred", "truncate", "dd", "mkfs", "mv",
        "chmod", "chown", "chgrp", "ln",
        "kill", "killall", "pkill",
        "shutdown", "reboot", "halt", "poweroff", "init", "systemctl", "service",
        "mount", "umount", "fdisk", "parted", "mkswap",
        "iptables", "nft", "ip", "ifconfig", "route",
        "useradd", "userdel", "usermod", "groupadd", "passwd", "chpasswd",
        "crontab", "at",
        "apt", "apt-get", "dpkg", "pacman", "yum", "dnf", "snap",
        "curl", "wget",
    }
)

# A prefix grant on any of these is a prefix grant on everything they can run.
_INTERPRETERS = frozenset(
    {
        "sh", "bash", "zsh", "dash", "ksh", "fish",
        "python", "python2", "python3", "perl", "ruby", "node", "npx", "deno",
        "eval", "exec", "source", ".",
        "env", "xargs", "nohup", "timeout", "watch",
        "sudo", "doas", "su",
        "ssh", "scp", "rsync",
        "make", "cmake",
    }
)

# Tools whose blast radius is not bounded by their name, so a grant for them must
# carry a scope:
#
#   Bash            arbitrary code
#   Write/Edit/…    the workspace is already auto-allowed, so an approval for one
#                   of these means a path OUTSIDE it — including ~/.gitconfig,
#                   whose core.sshCommand the forwarded ssh-agent depends on
#   WebFetch        an arbitrary outbound URL is an exfiltration channel
MUST_BE_SCOPED = frozenset(
    {"Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebFetch"}
)

_SUBJECT_KEYS = {
    "Bash": "command",
    "WebFetch": "url",
    "WebSearch": "query",
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
    "Glob": "pattern",
    "Grep": "pattern",
}


@dataclass(frozen=True)
class Grant:
    id: int
    tool_name: str
    pattern: str
    match_type: str
    created_by: str
    created_at: int
    use_count: int


@dataclass(frozen=True)
class Suggestion:
    """What to offer on the button, and what it would create."""

    match_type: str
    patterns: tuple[str, ...]

    @property
    def label(self) -> str:
        if self.match_type == MATCH_ANY:
            return "any use"
        if self.match_type == MATCH_EXACT:
            return "this exact command"
        return ", ".join(self.patterns)


def subject(tool_name: str, tool_input: object) -> str | None:
    """The string a grant is matched against, or None if the tool has none."""
    if not isinstance(tool_input, dict):
        return None
    key = _SUBJECT_KEYS.get(tool_name)
    if key is None:
        return None
    value = tool_input.get(key)
    return value if isinstance(value, str) and value else None


def split_segments(command: str) -> list[str]:
    """Split a shell command on operators, ignoring those inside quotes.

    Quote-aware so `echo "a && b"` stays one segment. A mis-split can only cause
    an extra segment that no grant covers, which denies — never the reverse.
    """
    segments: list[str] = []
    current: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            i += 1
            continue
        matched = next((op for op in _OPERATORS if command.startswith(op, i)), None)
        if matched:
            segments.append("".join(current).strip())
            current = []
            i += len(matched)
            continue
        current.append(ch)
        i += 1

    segments.append("".join(current).strip())
    return [s for s in segments if s]


def _prefix_of(segment: str) -> str | None:
    """The prefix a single command segment could be granted under."""
    tokens = segment.split()
    if not tokens:
        return None
    head = tokens[0]
    # A leading VAR=value hides the real command.
    if "=" in head:
        return None
    if head in _INTERPRETERS or head in _DESTRUCTIVE or "/" in head:
        # Interpreters run arbitrary code, destructive commands should never be
        # generalised from one sighting, and an absolute path is not a stable name.
        return None
    # Only extend to two words when the second is a plain bare word. Anything
    # quoted or path-like is an argument, not a subcommand, and `echo "a` is not a
    # prefix anyone means.
    if len(tokens) >= 2:
        second = tokens[1]
        if second and all(c.isalnum() or c in "-_" for c in second) \
           and not second.startswith("-"):
            return f"{head} {second}"
    return head


def _matches_one(grant: Grant, tool_name: str, text: str) -> bool:
    """Whether a single grant covers one subject string."""
    if grant.tool_name != tool_name:
        return False

    if grant.match_type == MATCH_EXACT:
        return text == grant.pattern

    if grant.match_type == MATCH_ANY:
        # Defensive: refuse a wildcard for a tool that must be scoped, even if such
        # a row reached the table by a hand-edit or a future bug. The code that
        # creates grants is not the only thing standing between a wildcard and
        # arbitrary code.
        return tool_name not in MUST_BE_SCOPED

    if not grant.pattern:
        return False

    if tool_name == "Bash":
        # A prefix grant never applies to something it cannot summarise.
        if any(seq in text for seq in _NO_PREFIX_SEQUENCES):
            return False
        if text == grant.pattern:
            return True
        # Boundary: `git status` must not match `git statusfoo`.
        return text.startswith(grant.pattern + " ")

    if text == grant.pattern:
        return True
    return text.startswith(grant.pattern) and (
        grant.pattern.endswith(("/", ":"))
        or text[len(grant.pattern):].startswith(("/", "?", " "))
    )


def covered_by(
    grants: list[Grant], tool_name: str, tool_input: object
) -> list[Grant] | None:
    """The grants covering this call, or None if a human must decide.

    For Bash this requires EVERY segment of a compound command to be covered.
    Granting `cd` and `npm test` covers `cd /x && npm test`; granting only `cd`
    does not.
    """
    if not tool_name or tool_name == "<unknown>":
        return None

    text = subject(tool_name, tool_input)

    if text is None:
        hit = next(
            (
                g
                for g in grants
                if g.tool_name == tool_name and g.match_type == MATCH_ANY
            ),
            None,
        )
        return [hit] if hit and tool_name not in MUST_BE_SCOPED else None

    if tool_name != "Bash":
        hit = next((g for g in grants if _matches_one(g, tool_name, text)), None)
        return [hit] if hit else None

    # An exact grant on the whole command short-circuits everything, and is the
    # only thing that can cover a command with substitution or redirection.
    exact = next(
        (
            g
            for g in grants
            if g.tool_name == tool_name
            and g.match_type == MATCH_EXACT
            and g.pattern == text
        ),
        None,
    )
    if exact:
        return [exact]

    if any(seq in text for seq in _NO_PREFIX_SEQUENCES):
        return None

    segments = split_segments(text)
    if not segments:
        return None

    used: list[Grant] = []
    for segment in segments:
        hit = next((g for g in grants if _matches_one(g, tool_name, segment)), None)
        if hit is None:
            return None
        used.append(hit)
    return used


def suggest(tool_name: str, tool_input: object) -> Suggestion | None:
    """What the "always allow" button should offer, or None if nothing safe."""
    if not tool_name or tool_name == "<unknown>":
        return None

    text = subject(tool_name, tool_input)

    if text is None:
        if tool_name in MUST_BE_SCOPED:
            return None
        return Suggestion(MATCH_ANY, (ANY,))

    if tool_name != "Bash":
        return Suggestion(MATCH_PREFIX, (text,))

    # Substitution, redirection, newlines: exact only. Still grantable, just not
    # generalisable.
    if any(seq in text for seq in _NO_PREFIX_SEQUENCES):
        return Suggestion(MATCH_EXACT, (text,))

    segments = split_segments(text)
    prefixes = [_prefix_of(s) for s in segments]
    if not segments or any(p is None for p in prefixes):
        # An interpreter, a VAR= assignment or an absolute path in there somewhere:
        # the command can still be granted, but only as itself.
        return Suggestion(MATCH_EXACT, (text,))

    # Dedupe while keeping order, so `cd x && cd y` offers `cd` once.
    seen: list[str] = []
    for p in prefixes:
        assert p is not None
        if p not in seen:
            seen.append(p)
    return Suggestion(MATCH_PREFIX, tuple(seen))
