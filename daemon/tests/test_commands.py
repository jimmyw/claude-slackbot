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
        return {"user": "bot", "user_id": "U_BOT", "bot_id": "B_BOT"}


def make_daemon(tmp: Path, allowed_users: frozenset[str] = frozenset()) -> Daemon:
    key = tmp / "key"
    key.write_text("x")
    config = Config(
        bot_token="xoxb-real", app_token="xapp-real",
        authorized_user=AUTHORIZED, allowed_users=allowed_users,
        vm_host="10.0.0.1", vm_user="agent", vm_ssh_key=key,
        vm_domain="agent-vm", vm_workdir="/home/agent/work",
        libvirt_uri="qemu:///system", forward_agent=False,
        agent_policy="permissive",
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

        print("\n[1] the registry discovers command modules")
        from slackagent.commands import commands as registered, registry
        names = [c.name for c in registered()]
        for expected in ["help", "status", "grants", "revoke"]:
            check(f"{expected!r} is registered", expected in names, names)
        check("aliases resolve", {"grant", "commands", "?"} <= set(registry()),
              sorted(registry()))
        check("every command has a summary",
              all(c.summary for c in registered()))
        check("every command has a usage line",
              all(c.usage.startswith("|") for c in registered()),
              [c.usage for c in registered()])

        print("\n[2] | commands are handled here, never sent to Claude")
        for text in ["|help", "|commands", "|?", "|", "|status", "|grants",
                     "|grant", "  |status  ", "|STATUS", "|Grants",
                     "|pause", "|resume", "|mute", "|unmute",
                     "|silent", "|quiet", "|mentions", "|SILENT"]:
            check(f"{text!r} is a command", await handled(text), text)

        print("\n[3] |revoke argument validation via argparse")
        for text in ["|revoke 3", "|revoke all", "|revoke *"]:
            check(f"{text!r} is a command", await handled(text), text)
        for bad, why in [("|revoke", "missing argument"),
                         ("|revoke xyz", "not an id"),
                         ("|revoke 3 4", "too many")]:
            client.posted.clear()
            check(f"{bad!r} is consumed ({why})", await handled(bad), bad)
            body = client.posted[-1]["text"]
            check(f"{bad!r} explains itself", "usage" in body.lower(), body[:70])
            check(f"{bad!r} says nothing went to Claude", "Claude" in body)

        print("\n[4] -h gives per-command help, not an error")
        for text in ["|grants -h", "|revoke -h", "|status --help"]:
            client.posted.clear()
            check(f"{text!r} is consumed", await handled(text), text)
            body = client.posted[-1]["text"]
            check(f"{text!r} shows usage", "usage:" in body.lower(), body[:70])
            check(f"{text!r} is not flagged as an error",
                  ":warning:" not in body, body[:70])

        print("\n[5] |grants has its own options")
        client.posted.clear()
        check("|grants --tool Bash works", await handled("|grants --tool Bash"))
        check("|grants --unused works", await handled("|grants --unused"))
        client.posted.clear()
        check("an unknown option is consumed", await handled("|grants --nope"))
        check("and explains itself",
              "usage" in client.posted[-1]["text"].lower(),
              client.posted[-1]["text"][:70])

        print("\n[6] an unparsed | line is consumed, NOT forwarded")
        for text in ["|grnats", "|dance", "||"]:
            client.posted.clear()
            check(f"{text!r} is consumed", await handled(text), text)
            body = client.posted[-1]["text"]
            check(f"{text!r} says nothing went to Claude", "Claude" in body, body[:70])
            check(f"{text!r} lists what is available",
                  "Available" in body or "help" in body, body[:70])

        print("\n[7] a | line anywhere means the message is not forwarded")
        for text in ["please run\n|status", "do a thing\n  |revoke all\nthanks"]:
            client.posted.clear()
            check(f"{text!r} is consumed", await handled(text), text)
            check("and says nothing was sent to Claude",
                  "Claude" in client.posted[-1]["text"], client.posted[-1]["text"][:70])

        print("\n[8] prose without a | reaches Claude, including the old keywords")
        for text in [
            "revoke the old deploy key from GitHub",
            "status of the build?",
            "grants in the repo are documented where?",
            "revoke 3", "status", "grants", "help",
            "pause the CI job while I look at the logs",
            "resume from where we left off", "pause", "resume",
            "be quiet for a bit", "silent", "quiet down the CI logs",
            "what does the | character do in bash?",
        ]:
            check(f"{text!r} goes to Claude", not await handled(text), text)

        print("\n[9] commands are operator-only")
        for text in ["|help", "|status", "|grants", "|revoke all"]:
            client.posted.clear()
            check(f"guest {text!r} is refused", await handled(text, operator=False), text)
            check("and the refusal names the operator",
                  AUTHORIZED in client.posted[-1]["text"], client.posted[-1]["text"][:70])
        check("a guest's ordinary request still reaches Claude",
              not await handled("please read the README", operator=False))

        print("\n[9b] |auth changes the policy at runtime")
        check("|auth shows the mode", await handled("|auth"))
        body = client.posted[-1]["text"]
        check("and names the current one",
              "permissive" in body or "strict" in body, body[:60])

        client.posted.clear()
        check("|auth strict is accepted", await handled("|auth strict"))
        check("the setting persisted",
              d._store.get_setting("agent_policy", "permissive") == "strict",  # noqa: SLF001
              d._store.get_setting("agent_policy", "permissive"))  # noqa: SLF001
        check("and it says when it applies",
              "next message" in client.posted[-1]["text"],
              client.posted[-1]["text"][:80])

        client.posted.clear()
        check("switching to the same mode is a no-op",
              await handled("|auth strict"))
        check("and says so", "already" in client.posted[-1]["text"],
              client.posted[-1]["text"][:60])

        client.posted.clear()
        check("|auth back to permissive", await handled("|auth permissive"))
        check("setting updated",
              d._store.get_setting("agent_policy", "strict") == "permissive")  # noqa: SLF001

        client.posted.clear()
        check("|auth open is accepted", await handled("|auth open"))
        check("open persisted",
              d._store.get_setting("agent_policy", "x") == "open")  # noqa: SLF001
        check("and it warns about what open means",
              ":warning:" in client.posted[-1]["text"],
              client.posted[-1]["text"][-120:])
        await handled("|auth permissive")

        client.posted.clear()
        check("|auth lists every mode", await handled("|auth"))
        body = client.posted[-1]["text"]
        for mode in ("open", "permissive", "strict"):
            check(f"the listing includes {mode!r}", f"`{mode}`" in body, body[:80])
        check("and marks the current one", "current" in body, body[:200])

        client.posted.clear()
        check("an invalid mode is consumed", await handled("|auth nonsense"))
        check("and argparse explains the choices",
              "invalid choice" in client.posted[-1]["text"],
              client.posted[-1]["text"][:80])
        check("and nothing went to Claude",
              "Claude" in client.posted[-1]["text"])

        check("the setting records who changed it",
              (d._store.setting_meta("agent_policy") or ("", ""))[1] == AUTHORIZED,  # noqa: SLF001
              d._store.setting_meta("agent_policy"))  # noqa: SLF001

        print("\n[10] |help lists every command with a description")
        client.posted.clear()
        await handled("|help")
        body = client.posted[-1]["text"]
        for command in registered():
            check(f"help lists |{command.name}", f"|{command.name}" in body)
            check(f"help shows {command.name}'s summary",
                  command.summary in body, command.summary)
        check("help points at per-command help", "-h" in body, body[-120:])

        d._store.close()  # noqa: SLF001

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
