#!/usr/bin/env python3
"""Render the cloud-init user-data for the agent VM.

Substitutes the two public keys and embeds every file from vm-files/ directly
into the seed, so the guest is fully provisioned on first boot.

Why embed rather than push afterwards: the daemon's key is pinned to a forced
command, so it cannot run rsync or get a shell. A post-boot push therefore has
nothing to push *with* on a fresh VM — the guest would be unreachable. Files go
in via the seed; the admin key exists for later updates and for the one-time
`claude setup-token`.

Files are base64-encoded (`encoding: b64`) so no content can break the YAML, and
they land in a root-owned staging directory first. write_files runs before
users-groups in cloud-init's init stage, so `owner: agent:agent` would fail at
that point; a late runcmd installs them with the right ownership instead.

Usage:
  render-cloud-init.py <repo-dir> <daemon-pubkey-file> <admin-pubkey-file>
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

STAGING = "/var/lib/agent-provision"

# (source under vm-files/, destination in the guest, mode, owner)
PAYLOAD = [
    ("usr/local/bin/agent-exec", "/usr/local/bin/agent-exec", "0755", "root:root"),
    (
        "home/agent/.claude/hooks/approve.py",
        "/home/agent/.claude/hooks/approve.py",
        "0755",
        "agent:agent",
    ),
    (
        "home/agent/.claude/settings.json",
        "/home/agent/.claude/settings.json",
        "0644",
        "agent:agent",
    ),
    ("home/agent/CLAUDE.md", "/home/agent/CLAUDE.md", "0644", "agent:agent"),
    (
        "home/agent/memory/MEMORY.md",
        "/home/agent/memory/MEMORY.md",
        "0644",
        "agent:agent",
    ),
]


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        return 64

    repo = Path(sys.argv[1])
    daemon_key = Path(sys.argv[2]).read_text().strip()
    admin_key = Path(sys.argv[3]).read_text().strip()

    template_path = repo / "bootstrap/cloud-init/user-data"
    if not template_path.is_file():
        print(f"missing cloud-init template: {template_path}", file=sys.stderr)
        return 1
    template = template_path.read_text()

    write_files: list[str] = []
    install_cmds: list[str] = []

    for source, dest, mode, owner in PAYLOAD:
        path = repo / "vm-files" / source
        if not path.is_file():
            print(f"missing payload file: {path}", file=sys.stderr)
            return 1
        encoded = base64.b64encode(path.read_bytes()).decode()
        staged = f"{STAGING}/{source}"

        write_files.append(
            "\n".join(
                [
                    f"  - path: {staged}",
                    "    encoding: b64",
                    "    permissions: '0600'",
                    f"    content: {encoded}",
                ]
            )
        )
        install_cmds.append(
            f"  - ['install', '-D', '-m', '{mode}', '-o', '{owner.split(':')[0]}', "
            f"'-g', '{owner.split(':')[1]}', '{staged}', '{dest}']"
        )

    rendered = (
        template.replace("@@AGENT_PUBKEY@@", daemon_key)
        .replace("@@ADMIN_PUBKEY@@", admin_key)
        .replace("@@WRITE_FILES@@", "\n".join(write_files))
        .replace("@@INSTALL_FILES@@", "\n".join(install_cmds))
    )

    for placeholder in ("@@AGENT_PUBKEY@@", "@@ADMIN_PUBKEY@@", "@@WRITE_FILES@@",
                        "@@INSTALL_FILES@@"):
        if placeholder in rendered:
            print(f"placeholder left unsubstituted: {placeholder}", file=sys.stderr)
            return 1

    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
