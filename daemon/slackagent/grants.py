"""Persistent "always allow" grants, and the matching that makes them safe.

A grant is (tool_name, prefix). It lives in the daemon's sqlite on the HOST, so
the agent cannot grant itself anything — it can only ask, and the answer is
computed out here.

The whole risk sits in the matching. A naive prefix check is trivially defeated:

    grant   "git status"
    command "git status; rm -rf ~"        -> starts with the prefix

so a command is only eligible if it contains nothing that can chain, pipe,
redirect, or substitute. Anything with those characters goes to a human, however
harmless its first two words look.

The second trap is the boundary: "git status" must not match "git statusfoo". A
grant matches only the exact command or the command followed by a space.
"""
from __future__ import annotations

from dataclasses import dataclass

# Characters that let one command become another. Rejecting these is what makes a
# prefix grant meaningful rather than decorative.
#
#   ;  &  |     chaining and backgrounding (covers && and ||)
#   `  $(       command substitution
#   >  <        redirection, including process substitution <( >(
#   newline     a second command on the next line
#   \           escaping and line continuation, used to obfuscate the above
_UNSAFE_SEQUENCES = (";", "&", "|", "`", "$(", ">", "<", "\n", "\r", "\\")

# The field that identifies what a tool is being asked to act on.
_SUBJECT_KEYS = {
    "Bash": "command",
    "WebFetch": "url",
    "WebSearch": "query",
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
}


@dataclass(frozen=True)
class Grant:
    id: int
    tool_name: str
    pattern: str
    created_by: str
    created_at: int
    use_count: int


def subject(tool_name: str, tool_input: object) -> str | None:
    """The string a grant is matched against, or None if there isn't one."""
    if not isinstance(tool_input, dict):
        return None
    key = _SUBJECT_KEYS.get(tool_name)
    if key is None:
        # An unknown tool has no agreed subject, so it can never be granted.
        # Failing closed here means a new tool type is gated until someone
        # deliberately teaches this module about it.
        return None
    value = tool_input.get(key)
    return value if isinstance(value, str) and value else None


def is_grantable(tool_name: str, tool_input: object) -> bool:
    """Whether this call could be covered by a prefix grant at all."""
    text = subject(tool_name, tool_input)
    if text is None:
        return False
    if tool_name == "Bash":
        return not any(seq in text for seq in _UNSAFE_SEQUENCES)
    return True


def matches(grant_tool: str, grant_pattern: str, tool_name: str, tool_input: object) -> bool:
    """Whether an existing grant covers this call.

    Deliberately re-checks is_grantable: a grant created for a simple command
    must not later authorise a compound one that happens to share its opening
    words.
    """
    if grant_tool != tool_name:
        return False
    if not is_grantable(tool_name, tool_input):
        return False
    text = subject(tool_name, tool_input)
    if text is None or not grant_pattern:
        return False
    if text == grant_pattern:
        return True
    # Boundary: "git status" must not match "git statusfoo". For paths and URLs a
    # trailing separator is the equivalent boundary.
    if tool_name == "Bash":
        return text.startswith(grant_pattern + " ")
    return text.startswith(grant_pattern) and (
        grant_pattern.endswith(("/", ":")) or text[len(grant_pattern):].startswith(("/", "?", " "))
    )


def suggest_pattern(tool_name: str, tool_input: object) -> str | None:
    """The prefix to offer as an 'always allow' button, or None if unsafe.

    For Bash, the command word plus one subcommand when that reads as one:
    `git status --short` -> `git status`, `ls -la /x` -> `ls`. Narrow enough to be
    meaningful, broad enough to be worth pressing.
    """
    if not is_grantable(tool_name, tool_input):
        return None
    text = subject(tool_name, tool_input)
    if text is None:
        return None

    if tool_name != "Bash":
        return text

    tokens = text.split()
    if not tokens:
        return None
    # A leading VAR=value assignment makes the real command ambiguous; do not guess.
    if "=" in tokens[0]:
        return None
    if len(tokens) >= 2 and not tokens[1].startswith("-"):
        # Only when the second token looks like a subcommand, not a path or file.
        if "/" not in tokens[1] and "." not in tokens[1]:
            return f"{tokens[0]} {tokens[1]}"
    return tokens[0]
