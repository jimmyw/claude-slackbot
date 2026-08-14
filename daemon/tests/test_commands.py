"""Command dispatch: which messages the daemon answers itself, and which reach Claude.

The risk here is not security but silence: `status`, `grants` and `revoke` are
ordinary English words, so a loose match swallows real requests and answers them
with a usage error. An earlier version used startswith("revoke"), which turned
"revoke the old deploy key from GitHub" into "Usage: revoke <id>".

Run:  .venv/bin/python -m tests.test_commands
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from slackagent.app import Daemon
from slackagent.config import Config

failures: list[str] = []

AUTHORIZED = "U_JIMMY"
GUEST = "U_BOB"


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


class FakeClient:
    def __init__(self) -> None:
        self.posted: list[dict] = []

    async def chat_postMessage(self, **kwargs):  # noqa: N802
        self.posted.append(kwargs)
        return {"ts": "1.1", "ok": True}

    async def chat_update(self, **kwargs):  # noqa: N802
        return {"ok": True}

    async def auth_test(self):
        return {"user": "bot", "user_id": "U_BOT"}


def make_daemon(tmp: Path) -> Daemon:
    key = tmp / "key"
    key.write_text("x")
    config = Config(
        bot_token="xoxb-real", app_token="xapp-real",
        authorized_user=AUTHORIZED, allowed_users=frozenset(),
        vm_host="10.0.0.1", vm_user="agent", vm_ssh_key=key,
        vm_domain="agent-vm", vm_workdir="/home/agent/work",
        libvirt_uri="qemu:///system", forward_agent=False,
        approval_host="127.0.0.1", approval_port=9100, approval_timeout_s=600,
        tunnel_port_low=9101, tunnel_port_high=9199,
        db_path=tmp / "s.sqlite3", update_interval_s=0.0,
    )
    daemon = Daemon(config)
    # AsyncApp.client is a read-only property backed by _async_client.
    daemon._app._async_client = FakeClient()  # noqa: SLF001
    return daemon


async def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        d = make_daemon(tmp)
        client = d._app.client  # noqa: SLF001

        async def handled(text: str, operator: bool = True) -> bool:
            return await d._handle_command(  # noqa: SLF001
                "C1", "1.1", text, AUTHORIZED if operator else GUEST, operator
            )

        print("\n[1] exact commands are handled here, not sent to Claude")
        for text in ["help", "commands", "?", "status", "grants", "grant",
                     "!status", "  status  ", "STATUS", "Grants"]:
            check(f"{text!r} is a command", await handled(text), text)

        print("\n[2] revoke only when it carries an id or 'all'")
        for text in ["revoke 3", "revoke all", "revoke *", "!revoke 12"]:
            check(f"{text!r} is a command", await handled(text), text)

        print("\n[3] prose that merely starts with a command word reaches Claude")
        for text in [
            "revoke the old deploy key from GitHub",
            "revoke my github token please",
            "status of the build?",
            "grants in the repo are documented where?",
            "can you check status and report",
            "revoke 3 keys from the server",          # two args, not an id
            "revoke",                                  # bare, no target
        ]:
            check(f"{text!r} goes to Claude", not await handled(text), text)

        print("\n[4] guests cannot revoke, but the message is still consumed")
        before = len(client.posted)
        check("guest 'revoke all' is handled (refused, not forwarded)",
              await handled("revoke all", operator=False))
        check("and the refusal names the operator",
              AUTHORIZED in client.posted[-1]["text"], client.posted[-1]["text"])
        check("a guest may still read grants", await handled("grants", operator=False))
        check("something was posted for each", len(client.posted) > before)

        print("\n[5] help lists the whole API")
        client.posted.clear()
        await handled("help")
        body = client.posted[-1]["text"]
        for token in ["status", "grants", "revoke <id>", "revoke all", "help"]:
            check(f"help documents {token!r}", token in body)
        check("help says these must be the whole message",
              "whole" in body.lower(), body[:60])
        check("help names who can approve", AUTHORIZED in body)

        d._store.close()  # noqa: SLF001

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
