"""The private vm-files.local/ overlay: what the merged guest tree contains.

The overlay exists so operator-specific content stays out of the repository, and the only
way that works is if the build output is the same either way. So this pins both
directions: no overlay gives exactly the shared files, and an overlay changes exactly
what it says and nothing else. It also runs the real renderer, since a fresh VM is the
path that is easiest to forget.

Run:  .venv/bin/python -m tests.test_vmfiles_overlay
"""
from __future__ import annotations

import base64
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bootstrap"))
from vmfiles import stage  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def fake_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    (repo / "vm-files/home/agent/memory").mkdir(parents=True)
    (repo / "vm-files/etc").mkdir(parents=True)
    (repo / "vm-files/home/agent/CLAUDE.md").write_text("# Generic\n\nbase rules\n")
    (repo / "vm-files/home/agent/memory/MEMORY.md").write_text("# index\n")
    (repo / "vm-files/etc/gate.json").write_text("{}\n")
    return repo


def main() -> int:
    print("[1] merging")
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        repo = fake_repo(tmp)

        notes = stage(repo, tmp / "plain")
        check("with no overlay the tree is exactly vm-files/",
              tree(tmp / "plain") == tree(repo / "vm-files"))
        check("and nothing is reported", notes == [], notes)

        local = repo / "vm-files.local/home/agent"
        local.mkdir(parents=True)
        (local / "CLAUDE.append.md").write_text("## This deployment\n\nsecret project\n")
        (local / "NOTES.md").write_text("private notes\n")
        (repo / "vm-files.local/etc").mkdir()
        (repo / "vm-files.local/etc/gate.json").write_text('{"local": true}\n')

        notes = stage(repo, tmp / "merged")
        merged = tree(tmp / "merged")
        claude = merged["home/agent/CLAUDE.md"].decode()
        check("CLAUDE.append.md is appended, generic part first",
              claude == "# Generic\n\nbase rules\n\n## This deployment\n\nsecret project\n",
              claude)
        check("the append file itself is not shipped as a separate file",
              "home/agent/CLAUDE.append.md" not in merged)
        check("a same-path file replaces the shared one",
              merged["etc/gate.json"] == b'{"local": true}\n')
        check("a new file is added", merged["home/agent/NOTES.md"] == b"private notes\n")
        check("untouched files are untouched",
              merged["home/agent/memory/MEMORY.md"] == b"# index\n")
        check("the overlay reports what it did",
              sorted(notes) == sorted([
                  "replaces etc/gate.json", "adds home/agent/NOTES.md",
                  "appends home/agent/CLAUDE.append.md to home/agent/CLAUDE.md"]),
              notes)
        check("the shared tree on disk is never modified",
              (repo / "vm-files/home/agent/CLAUDE.md").read_text()
              == "# Generic\n\nbase rules\n")

    print("\n[2] the repository stays generic")
    tracked = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True,
                             text=True, check=True).stdout.split()
    check("no private overlay file is tracked",
          not [t for t in tracked if t.startswith("vm-files.local/")])
    check("the operator's key file is not tracked",
          "bootstrap/operator-keys.pub" not in tracked)
    ignored = subprocess.run(
        ["git", "check-ignore", "vm-files.local/x", "bootstrap/operator-keys.pub",
         "CLAUDE.local.md"], cwd=REPO, capture_output=True, text=True).stdout.split()
    check("all three private paths are gitignored", len(ignored) == 3, ignored)

    print("\n[3] a fresh VM goes through the same merge")
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        for name in ("daemon", "admin"):
            (tmp / f"{name}.pub").write_text(f"ssh-ed25519 AAAA{name} test\n")
        out = subprocess.run(
            [sys.executable, str(REPO / "bootstrap/render-cloud-init.py"), str(REPO),
             str(tmp / "daemon.pub"), str(tmp / "admin.pub")],
            capture_output=True, text=True)
        check("the renderer runs", out.returncode == 0, out.stderr[-300:])
        staged = {m.group(1): base64.b64decode(m.group(2)).decode()
                  for m in re.finditer(
                      r"path: /var/lib/agent-provision/(\S+)\n\s+encoding: b64\n"
                      r"\s+permissions: '0600'\n\s+content: (\S+)", out.stdout)}
        generic = (REPO / "vm-files/home/agent/CLAUDE.md").read_text()
        check("the rendered guest CLAUDE.md starts with the shared one",
              staged.get("home/agent/CLAUDE.md", "").startswith(generic.rstrip("\n")))
        append = REPO / "vm-files.local/home/agent/CLAUDE.append.md"
        if append.is_file():
            check("and carries the private section when one exists",
                  append.read_text().strip() in staged["home/agent/CLAUDE.md"])
        else:
            print("  (no local overlay on this machine; nothing more to check)")

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
