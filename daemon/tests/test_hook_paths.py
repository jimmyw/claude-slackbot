"""Workspace-containment tests for the approval hook.

The hook auto-allows writes inside /home/agent/work so documentation work is not
one Slack button press per file. That shortcut is only safe if "inside" cannot be
faked, so this exercises the escapes an agent could actually reach for: `..`
traversal, planted symlinks, sibling directories with a shared prefix, relative
paths, and tools whose blast radius is wider than the path they name.

Uses real directories and real symlinks in a tmpdir — the whole point is that
os.path.realpath behaves as assumed, which a mocked filesystem would not prove.

Run:  .venv/bin/python -m tests.test_hook_paths
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "vm-files/etc/claude-agent/approve.py"

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


def load_hook():
    spec = importlib.util.spec_from_file_location("approve_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main() -> int:
    hook = load_hook()

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        work = root / "work"
        (work / "repo" / "docs").mkdir(parents=True)
        (work / "repo" / "docs" / "index.md").write_text("x")

        # A sibling whose name shares the workspace prefix. A naive startswith()
        # check treats this as inside the workspace; it is not.
        sibling = root / "workshop"
        sibling.mkdir()
        (sibling / "secret.txt").write_text("s")

        outside = root / "etc"
        outside.mkdir()
        (outside / "passwd").write_text("p")

        # A symlink planted inside the workspace pointing out of it — the most
        # direct escape available to something that can already write here.
        (work / "escape").symlink_to(outside)
        (work / "escape_file").symlink_to(outside / "passwd")

        ws = str(work)

        print("\n[1] legitimate workspace writes are allowed")
        for path in [
            str(work / "notes.md"),
            str(work / "repo" / "docs" / "index.md"),
            str(work / "repo" / "docs" / "new-file-that-does-not-exist-yet.md"),
            str(work / "repo" / "deep" / "nested" / "new.md"),
            ws,
        ]:
            check(f"inside: ...{path[len(str(root)):]}",
                  hook.inside_workspace(path, ws), path)

        print("\n[2] escapes must NOT be treated as inside")
        escapes = {
            "parent traversal": str(work / ".." / "etc" / "passwd"),
            "deep traversal": str(work / "repo" / ".." / ".." / "etc" / "passwd"),
            "symlinked directory": str(work / "escape" / "passwd"),
            "symlinked file": str(work / "escape_file"),
            "sibling with shared prefix": str(sibling / "secret.txt"),
            "absolute elsewhere": "/etc/shadow",
            "the gate's own script": "/etc/claude-agent/approve.py",
            "the gate's settings": "/etc/claude-agent/settings.json",
            "the forced command": "/usr/local/bin/agent-exec",
            "the agent's token": "/home/agent/.config/claude-agent/token",
            "the agent's ssh keys": "/home/agent/.ssh/authorized_keys",
        }
        for label, path in escapes.items():
            check(f"blocked: {label}", not hook.inside_workspace(path, ws), path)

        print("\n[3] ambiguous or malformed paths are not 'inside'")
        for label, value in [
            ("relative path", "notes.md"),
            ("relative traversal", "../etc/passwd"),
            ("empty string", ""),
            ("None", None),
            ("an int", 5),
            ("a dict", {"file_path": str(work / "x.md")}),
        ]:
            check(f"not inside: {label}", not hook.inside_workspace(value, ws), value)

        print("\n[4] only single-file write tools qualify for the shortcut")
        for tool in ["Write", "Edit", "MultiEdit"]:
            got = hook.workspace_write_target(tool, {"file_path": "/a/b.md"})
            check(f"{tool} yields its path", got == "/a/b.md", got)
        got = hook.workspace_write_target(
            "NotebookEdit", {"notebook_path": "/a/n.ipynb"}
        )
        check("NotebookEdit yields notebook_path", got == "/a/n.ipynb", got)

        # These must never be shortcut: their effect is not bounded by a path.
        for tool in ["Bash", "WebFetch", "Task", "BashOutput", "KillShell",
                     "WebSearch", "Skill", "SlashCommand"]:
            got = hook.workspace_write_target(tool, {"file_path": str(work / "x")})
            check(f"{tool} is NOT eligible for the shortcut", got is None, got)

        print("\n[5] malformed tool_input never yields a target")
        for value in [None, "string", 42, [], {}, {"other_key": "/a/b"},
                      {"file_path": ""}, {"file_path": None}, {"file_path": 7}]:
            got = hook.workspace_write_target("Write", value)
            check(f"no target from {value!r}", got is None, got)

        print("\n[6] the real workspace constant points where we think")
        check("WORKSPACE is /home/agent/work",
              hook.WORKSPACE == "/home/agent/work", hook.WORKSPACE)
        check("Bash is not on the read-only allowlist",
              "Bash" not in hook.AUTO_ALLOW, sorted(hook.AUTO_ALLOW))
        check("Bash is not a workspace-write tool",
              "Bash" not in hook.WORKSPACE_WRITE_TOOLS,
              sorted(hook.WORKSPACE_WRITE_TOOLS))
        check("Edit is not on the read-only allowlist (it must be path-checked)",
              "Edit" not in hook.AUTO_ALLOW, sorted(hook.AUTO_ALLOW))

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
