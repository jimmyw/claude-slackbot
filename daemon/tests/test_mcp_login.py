"""`|mcp login` — a Slack modal is the only place a pasted credential ever appears.

Nothing here goes over a real Slack connection: a fake client records what would
have been sent (button messages, views_open calls, confirmations) so the tests can
assert the pasted value reaches sqlite and nowhere else — never a chat message,
never a log line, never the confirmation text.

Run:  .venv/bin/python -m tests.test_mcp_login
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from slackagent.config import Config
from slackagent.mcpconfig import Registry
from slackagent.mcp_login import ACTION_LOGIN_START, VIEW_LOGIN_MODAL, McpLoginService
from slackagent.mcpproxy import McpProxy
from slackagent.store import Store

OPERATOR = "U_OPERATOR"
GUEST = "U_BOB"
SECRET = "sess=abc123; very-secret-cookie-value"

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


def document() -> dict:
    return {
        "servers": {
            "syslog": {
                "type": "http", "url": "https://syslog.example/mcp",
                "credential": {"mode": "shared", "headers": {"Cookie": "stale"}},
                "tools": {"allow": ["query_logs"]},
            },
            "esp-crash": {
                "type": "http", "url": "https://esp.example/mcp",
                "credential": {
                    "mode": "shared",
                    "oauth": {"token_url": "https://esp.example/token",
                              "refresh_token": "r0"},
                },
                "tools": {"allow": ["get_crash"]},
            },
            "varys": {
                "type": "stdio", "command": sys.executable, "args": ["-c", "pass"],
                "credential": {"mode": "shared", "env": {}},
                "tools": {"allow": ["pulse"]},
            },
        }
    }


def write_config(tmp: Path) -> Path:
    path = tmp / "mcp.json"
    path.write_text(json.dumps(document()))
    os.chmod(path, 0o600)
    return path


def make_config(tmp: Path) -> Config:
    return Config(
        bot_token="xoxb-x", app_token="xapp-x", authorized_user=OPERATOR,
        allowed_users=frozenset(), vm_host="10.0.0.1", vm_user="agent",
        vm_ssh_key=tmp / "key", vm_domain="d", vm_workdir="/w",
        libvirt_uri="qemu:///system", forward_agent=False,
        agent_policy="permissive", approval_host="127.0.0.1",
        approval_port=9100, approval_timeout_s=5, tunnel_port_low=1,
        tunnel_port_high=2, db_path=tmp / "state.sqlite3",
        update_interval_s=0.0, mcp_config=tmp / "mcp.json", mcp_port=0,
    )


class FakeSlack:
    """Records what would have been sent, instead of calling Slack."""

    def __init__(self) -> None:
        self.posted: list[dict] = []
        self.opened: list[dict] = []

    async def chat_postMessage(self, **kwargs):  # noqa: ANN003, N802
        self.posted.append(kwargs)
        return {"ok": True}

    async def views_open(self, **kwargs):  # noqa: ANN003, N802
        self.opened.append(kwargs)
        return {"ok": True}


def button_value(slack: FakeSlack) -> dict:
    blocks = slack.posted[-1]["blocks"]
    actions = next(b for b in blocks if b["type"] == "actions")
    return json.loads(actions["elements"][0]["value"])


def submit_body(private_metadata: str, value: str, user: str) -> dict:
    return {
        "user": {"id": user},
        "view": {
            "private_metadata": private_metadata,
            "state": {"values": {"cred": {"value": {"value": value}}}},
        },
    }


async def test_ineligible_servers() -> None:
    print("\n[1] login only applies to a header-credentialed HTTP server")
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        write_config(tmp)
        registry = Registry(tmp / "mcp.json")
        store = Store(tmp / "s.sqlite3")
        slack = FakeSlack()
        service = McpLoginService(make_config(tmp), store, registry, slack)

        reason = await service.request("C1", "1.1", "esp-crash", OPERATOR)
        check("oauth servers are refused — they already self-refresh",
              "OAuth" in reason, reason)
        reason = await service.request("C1", "1.1", "varys", OPERATOR)
        check("stdio servers are refused — no HTTP header to log in with",
              "stdio" in reason, reason)
        reason = await service.request("C1", "1.1", "nosuch", OPERATOR)
        check("an unknown server is reported, not a crash", "no MCP server" in reason,
              reason)
        check("none of these posted a button", slack.posted == [], slack.posted)
        store.close()


async def test_happy_path() -> None:
    print("\n[2] the button, the modal, and the write — end to end")
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        write_config(tmp)
        registry = Registry(tmp / "mcp.json")
        store = Store(tmp / "s.sqlite3")
        slack = FakeSlack()
        service = McpLoginService(make_config(tmp), store, registry, slack)

        reason = await service.request("C1", "1.1", "syslog", OPERATOR)
        check("a header-based server is accepted", reason == "", reason)
        check("exactly one button message went out", len(slack.posted) == 1,
              slack.posted)
        check("the button message never contains a secret",
              SECRET not in json.dumps(slack.posted), slack.posted)

        blocks = slack.posted[-1]["blocks"]
        actions = next(b for b in blocks if b["type"] == "actions")
        check("the button uses the registered action id",
              actions["elements"][0]["action_id"] == ACTION_LOGIN_START, actions)

        value = button_value(slack)
        await service.handle_button(
            {"user": {"id": OPERATOR}, "trigger_id": "t1"},
            {"value": json.dumps(value)},
        )
        check("clicking as the operator opens exactly one modal",
              len(slack.opened) == 1, slack.opened)
        view = slack.opened[0]["view"]
        check("the modal uses the registered callback id",
              view["callback_id"] == VIEW_LOGIN_MODAL, view)
        check("the modal carries no secret either", SECRET not in json.dumps(view),
              view)

        result = await service.handle_submit(
            submit_body(view["private_metadata"], SECRET, OPERATOR)
        )
        check("submitting clears the modal",
              result == {"response_action": "clear"}, result)
        check("the value landed in the store, keyed to the server",
              store.mcp_header_override("syslog") == {"Cookie": SECRET})
        check("exactly one confirmation was posted, in addition to the button",
              len(slack.posted) == 2, slack.posted)
        check("the confirmation names who did it, but never the value",
              OPERATOR in slack.posted[-1]["text"]
              and SECRET not in slack.posted[-1]["text"], slack.posted[-1])

        store.close()


async def test_refusals() -> None:
    print("\n[3] only the operator can open or complete the modal")
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        write_config(tmp)
        registry = Registry(tmp / "mcp.json")
        store = Store(tmp / "s.sqlite3")
        slack = FakeSlack()
        service = McpLoginService(make_config(tmp), store, registry, slack)

        await service.request("C1", "1.1", "syslog", OPERATOR)
        value = button_value(slack)

        await service.handle_button(
            {"user": {"id": GUEST}, "trigger_id": "t1"},
            {"value": json.dumps(value)},
        )
        check("a guest clicking the button opens no modal", slack.opened == [],
              slack.opened)

        await service.handle_button(
            {"user": {"id": OPERATOR}, "trigger_id": "t1"},
            {"value": json.dumps(value)},
        )
        view = slack.opened[0]["view"]

        result = await service.handle_submit(
            submit_body(view["private_metadata"], SECRET, GUEST)
        )
        check("a submission claiming to be someone else is refused",
              result == {}, result)
        check("and nothing was written", store.mcp_header_override("syslog") is None)

        result = await service.handle_submit(
            submit_body(view["private_metadata"], "", OPERATOR)
        )
        check("an empty paste is rejected with a field error, not silently accepted",
              result.get("response_action") == "errors", result)

        store.close()


async def test_proxy_prefers_override() -> None:
    print("\n[4] the proxy's default upstream prefers a login over the file")
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        write_config(tmp)
        registry = Registry(tmp / "mcp.json")
        store = Store(tmp / "s.sqlite3")
        config = make_config(tmp)
        proxy = McpProxy(config, store, registry, lambda token: None)

        server = registry.get("syslog")
        credential = server.credential_for(OPERATOR)
        upstream = proxy._default_upstream(server, credential)  # noqa: SLF001
        check("with no override, the config file's header is used",
              upstream._headers["Cookie"] == "stale", upstream._headers)  # noqa: SLF001

        store.save_mcp_header_override("syslog", "", {"Cookie": SECRET}, OPERATOR)
        upstream = proxy._default_upstream(server, credential)  # noqa: SLF001
        check("once a login exists, it wins over the file",
              upstream._headers["Cookie"] == SECRET, upstream._headers)  # noqa: SLF001

        store.close()


async def test_logout() -> None:
    print("\n[5] |mcp logout clears a login")
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        store = Store(tmp / "s.sqlite3")
        check("clearing something that was never set changes nothing",
              store.clear_mcp_header_override("syslog") is False)
        store.save_mcp_header_override("syslog", "", {"Cookie": SECRET}, OPERATOR)
        check("clearing a real override reports the change",
              store.clear_mcp_header_override("syslog") is True)
        check("and it is actually gone",
              store.mcp_header_override("syslog") is None)
        store.close()


async def main() -> int:
    await test_ineligible_servers()
    await test_happy_path()
    await test_refusals()
    await test_proxy_prefers_override()
    await test_logout()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
