#!/usr/bin/env python3
"""Build the tree that is shipped into the guest: `vm-files/` plus a private overlay.

    vmfiles.py <repo-dir> <dest-dir>

`vm-files/` holds only what is true of any deployment. Anything specific to one
operator -- the project the agent works on, product notes, internal hostnames -- goes in
`vm-files.local/`, which is gitignored and mirrors the same layout:

  * a file at the same path REPLACES the one in `vm-files/`;
  * a file that does not exist in `vm-files/` is added;
  * `home/agent/CLAUDE.append.md` is APPENDED to the guest `CLAUDE.md` instead of
    replacing it, which is the common case: the generic behaviour stays in the repo
    and the local file only says what this agent is for.

Both `render-cloud-init.py` (a fresh VM) and `30-install-vm-files.sh` (an existing one)
call this, so the two paths cannot disagree about what a guest contains.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

OVERLAY = "vm-files.local"
APPEND = Path("home/agent/CLAUDE.append.md")
APPEND_TARGET = Path("home/agent/CLAUDE.md")


def stage(repo: Path, dest: Path) -> list[str]:
    """Write the merged tree into `dest` (which must not exist). Returns what the
    overlay changed, as human-readable lines, so callers can say so."""
    base = repo / "vm-files"
    if not base.is_dir():
        raise SystemExit(f"missing {base}")
    shutil.copytree(base, dest)

    notes: list[str] = []
    overlay = repo / OVERLAY
    if not overlay.is_dir():
        return notes

    for path in sorted(p for p in overlay.rglob("*") if p.is_file()):
        rel = path.relative_to(overlay)
        if rel == APPEND:
            continue
        target = dest / rel
        verb = "replaces" if target.exists() else "adds"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        notes.append(f"{verb} {rel}")

    append = overlay / APPEND
    if append.is_file():
        target = dest / APPEND_TARGET
        text = target.read_text().rstrip("\n") + "\n\n" + append.read_text().lstrip("\n")
        target.write_text(text if text.endswith("\n") else text + "\n")
        notes.append(f"appends {APPEND} to {APPEND_TARGET}")
    return notes


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 64
    for note in stage(Path(sys.argv[1]), Path(sys.argv[2])):
        print(f"    overlay {note}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
