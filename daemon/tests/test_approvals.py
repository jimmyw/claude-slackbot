"""Integration test for the approval gate.

Exercises the whole loop with a fake Slack client and no VM: hook POST -> Slack
message -> button click -> verdict returned to the blocked hook. The cases that
matter are the security ones — an unauthorized clicker must not be able to
approve, and a timeout must deny.

Run:  .venv/bin/python -m tests.test_approvals
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from slackagent.approvals import ACTION_APPROVE, ACTION_DENY, ApprovalService
from slackagent.config import Config
from slackagent.store import Store

AUTHORIZED = "U_JIMMY"
INTRUDER = "U_SOMEONE_ELSE"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


class FakeSlack:
    """Records what the daemon would have sent to Slack."""

    def __init__(self) -> None:
        self.posted: list[dict] = []
        self.updated: list[dict] = []
        self._counter = 0

    async def chat_postMessage(self, **kwargs):  # noqa: N802
        self._counter += 1
        ts = f"1700000000.{self._counter:06d}"
        self.posted.append({**kwargs, "ts": ts})
        return {"ts": ts, "ok": True}

    async def chat_update(self, **kwargs):  # noqa: N802
        self.updated.append(kwargs)
        return {"ok": True}


class FakeRespond:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, payload):
        self.messages.append(payload)


def make_config(db_path: Path, port: int, timeout_s: int) -> Config:
    key = db_path.parent / "fake_key"
    key.write_text("not a real key")
    return Config(
        bot_token="xoxb-fake",
        app_token="xapp-fake",
        authorized_user=AUTHORIZED,
        vm_host="127.0.0.1",
        vm_user="agent",
        vm_ssh_key=key,
        vm_domain="agent-vm",
        vm_workdir="/home/agent/work",
        libvirt_uri="qemu:///system",
        forward_agent=False,
        approval_host="127.0.0.1",
        approval_port=port,
        approval_timeout_s=timeout_s,
        tunnel_port_low=9101,
        tunnel_port_high=9199,
        db_path=db_path,
        update_interval_s=0.0,
    )


async def post_approve(port: int, payload: dict, timeout: float = 30.0) -> dict:
    """Stand in for the VM hook: POST and block on the verdict."""

    def _blocking() -> dict:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/approve",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            return {"http_error": exc.code}

    return await asyncio.to_thread(_blocking)


def click(action_id: str, approval_id: str, user: str) -> tuple[dict, dict]:
    return (
        {"user": {"id": user}},
        {"action_id": action_id, "value": approval_id},
    )


async def scenario_authorized_approve(tmp: Path) -> None:
    print("\n[1] authorized user approves")
    store = Store(tmp / "a.sqlite3")
    slack = FakeSlack()
    config = make_config(tmp / "a.sqlite3", 19101, timeout_s=30)
    service = ApprovalService(config, store, slack)
    await service.start()
    try:
        service.register_run("tok1", "C1", "111.1", "sess-1")
        pending = asyncio.create_task(
            post_approve(
                19101,
                {
                    "run_token": "tok1",
                    "tool_name": "Write",
                    "tool_input": {"file_path": "/home/agent/work/x.txt",
                                   "content": "hello"},
                    "tool_use_id": "toolu_1",
                },
            )
        )

        await asyncio.sleep(0.4)
        check("approval message posted to the thread", len(slack.posted) == 1)
        blocks = slack.posted[0].get("blocks") or []
        actions = [b for b in blocks if b.get("type") == "actions"]
        check("message carries Approve/Deny buttons", len(actions) == 1)
        approval_id = actions[0]["elements"][0]["value"]
        check("thread_ts matches the run", slack.posted[0]["thread_ts"] == "111.1")

        body, action = click(ACTION_APPROVE, approval_id, AUTHORIZED)
        await service.handle_button(body, action, FakeRespond())

        verdict = await asyncio.wait_for(pending, timeout=5)
        check("hook received approved=True", verdict.get("approved") is True, verdict)
        row = store.approval(approval_id)
        check("audit row says approved", row["state"] == "approved", row["state"])
        check("audit row records who", row["resolved_by"] == AUTHORIZED)
        check("buttons replaced after decision", len(slack.updated) == 1)
    finally:
        await service.stop()
        store.close()


async def scenario_unauthorized_click(tmp: Path) -> None:
    print("\n[2] unauthorized user clicks Approve  <-- the access control")
    store = Store(tmp / "b.sqlite3")
    slack = FakeSlack()
    config = make_config(tmp / "b.sqlite3", 19102, timeout_s=30)
    service = ApprovalService(config, store, slack)
    await service.start()
    try:
        service.register_run("tok2", "C1", "222.2", "sess-2")
        pending = asyncio.create_task(
            post_approve(
                19102,
                {"run_token": "tok2", "tool_name": "Bash",
                 "tool_input": {"command": "rm -rf /"}, "tool_use_id": "toolu_2"},
            )
        )
        await asyncio.sleep(0.4)
        blocks = slack.posted[0]["blocks"]
        approval_id = [b for b in blocks if b["type"] == "actions"][0][
            "elements"
        ][0]["value"]

        respond = FakeRespond()
        body, action = click(ACTION_APPROVE, approval_id, INTRUDER)
        await service.handle_button(body, action, respond)

        check("intruder got an ephemeral refusal", len(respond.messages) == 1)
        check(
            "refusal is ephemeral only",
            respond.messages[0].get("response_type") == "ephemeral",
        )
        row = store.approval(approval_id)
        check("approval STILL pending after intruder click",
              row["state"] == "pending", row["state"])
        check("hook has not been unblocked", not pending.done())

        # The real operator can still decide afterwards.
        body, action = click(ACTION_DENY, approval_id, AUTHORIZED)
        await service.handle_button(body, action, FakeRespond())
        verdict = await asyncio.wait_for(pending, timeout=5)
        check("operator's deny reached the hook", verdict.get("approved") is False)
        check("audit row says denied", store.approval(approval_id)["state"] == "denied")
    finally:
        await service.stop()
        store.close()


async def scenario_timeout(tmp: Path) -> None:
    print("\n[3] nobody answers  ->  deny")
    store = Store(tmp / "c.sqlite3")
    slack = FakeSlack()
    config = make_config(tmp / "c.sqlite3", 19103, timeout_s=1)
    service = ApprovalService(config, store, slack)
    await service.start()
    try:
        service.register_run("tok3", "C1", "333.3", "sess-3")
        verdict = await post_approve(
            19103,
            {"run_token": "tok3", "tool_name": "Edit",
             "tool_input": {"file_path": "/etc/passwd"}, "tool_use_id": "toolu_3"},
        )
        check("timeout denies", verdict.get("approved") is False, verdict)
        check("reason mentions the window", "no answer" in verdict.get("reason", ""))
        blocks = slack.posted[0]["blocks"]
        approval_id = [b for b in blocks if b["type"] == "actions"][0][
            "elements"
        ][0]["value"]
        check("audit row says timeout",
              store.approval(approval_id)["state"] == "timeout")
    finally:
        await service.stop()
        store.close()


async def scenario_unknown_token(tmp: Path) -> None:
    print("\n[4] unknown run token  ->  403, nothing posted")
    store = Store(tmp / "d.sqlite3")
    slack = FakeSlack()
    config = make_config(tmp / "d.sqlite3", 19104, timeout_s=5)
    service = ApprovalService(config, store, slack)
    await service.start()
    try:
        verdict = await post_approve(
            19104,
            {"run_token": "nope", "tool_name": "Bash",
             "tool_input": {"command": "id"}, "tool_use_id": "t"},
        )
        check("rejected with 403", verdict.get("http_error") == 403, verdict)
        check("nothing posted to Slack", len(slack.posted) == 0)
    finally:
        await service.stop()
        store.close()


async def scenario_double_click(tmp: Path) -> None:
    print("\n[5] double click  ->  second is a no-op")
    store = Store(tmp / "e.sqlite3")
    slack = FakeSlack()
    config = make_config(tmp / "e.sqlite3", 19105, timeout_s=30)
    service = ApprovalService(config, store, slack)
    await service.start()
    try:
        service.register_run("tok5", "C1", "555.5", "sess-5")
        pending = asyncio.create_task(
            post_approve(
                19105,
                {"run_token": "tok5", "tool_name": "Write",
                 "tool_input": {"file_path": "/tmp/x"}, "tool_use_id": "toolu_5"},
            )
        )
        await asyncio.sleep(0.4)
        blocks = slack.posted[0]["blocks"]
        approval_id = [b for b in blocks if b["type"] == "actions"][0][
            "elements"
        ][0]["value"]

        body, action = click(ACTION_APPROVE, approval_id, AUTHORIZED)
        await service.handle_button(body, action, FakeRespond())
        await asyncio.wait_for(pending, timeout=5)

        respond = FakeRespond()
        body, action = click(ACTION_DENY, approval_id, AUTHORIZED)
        await service.handle_button(body, action, respond)
        check("second click told it's already resolved",
              any("Already resolved" in m.get("text", "") for m in respond.messages),
              respond.messages)
        check("state unchanged by the second click",
              store.approval(approval_id)["state"] == "approved")
    finally:
        await service.stop()
        store.close()


async def scenario_run_ends_first(tmp: Path) -> None:
    print("\n[6] run dies while an approval is waiting  ->  deny, no hang")
    store = Store(tmp / "f.sqlite3")
    slack = FakeSlack()
    config = make_config(tmp / "f.sqlite3", 19106, timeout_s=30)
    service = ApprovalService(config, store, slack)
    await service.start()
    try:
        service.register_run("tok6", "C1", "666.6", "sess-6")
        pending = asyncio.create_task(
            post_approve(
                19106,
                {"run_token": "tok6", "tool_name": "Bash",
                 "tool_input": {"command": "sleep 999"}, "tool_use_id": "toolu_6"},
            )
        )
        await asyncio.sleep(0.4)
        service.unregister_run("tok6")
        verdict = await asyncio.wait_for(pending, timeout=5)
        check("waiting hook was released with a deny",
              verdict.get("approved") is False, verdict)
        check("reason explains the run ended",
              "run ended" in verdict.get("reason", ""), verdict)
    finally:
        await service.stop()
        store.close()


async def scenario_session_mapping(tmp: Path) -> None:
    print("\n[7] thread -> session mapping")
    store = Store(tmp / "g.sqlite3")
    try:
        first = store.get_or_create_session("C1", "777.7", "uuid-aaa")
        check("new thread gets the minted uuid", first.session_id == "uuid-aaa")
        check("new thread uses --session-id (not resume)", first.is_new)

        store.mark_session_created("C1", "777.7")
        second = store.get_or_create_session("C1", "777.7", "uuid-bbb")
        check("reply reuses the original session", second.session_id == "uuid-aaa")
        check("reply uses --resume", not second.is_new)

        other = store.get_or_create_session("C1", "888.8", "uuid-ccc")
        check("a different thread is a different session",
              other.session_id == "uuid-ccc")

        # Regression: an in-thread reply in a thread we do not own must be a
        # pure read. A get-or-create here would claim the thread with a bogus
        # session id and poison it permanently.
        check("find_session on an unknown thread returns None",
              store.find_session("C1", "999.9") is None)
        check("find_session did not create a row",
              store.find_session("C1", "999.9") is None)
        check("find_session on a known thread returns it",
              (store.find_session("C1", "777.7") or first).session_id == "uuid-aaa")

        # Regression, both directions of a genuinely nasty pair:
        #
        #  * a run that never started must stay --session-id, or we --resume a
        #    session that does not exist;
        #  * a run that emitted `init` and THEN died must switch to --resume,
        #    because claude rejects --session-id for an existing id with
        #    "Session ID ... is already in use" — verified against 2.1.231 — which
        #    would break the thread permanently.
        never_started = store.get_or_create_session("C2", "aaa.1", "uuid-ddd")
        check("a run that never started stays on --session-id",
              never_started.is_new)

        store.get_or_create_session("C3", "bbb.1", "uuid-eee")
        store.mark_session_created("C3", "bbb.1")   # init seen, then the run dies
        crashed = store.get_or_create_session("C3", "bbb.1", "uuid-fff")
        check("a run that emitted init then died switches to --resume",
              not crashed.is_new)
        check("and keeps its original session id",
              crashed.session_id == "uuid-eee")
    finally:
        store.close()


async def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        await scenario_authorized_approve(tmp)
        await scenario_unauthorized_click(tmp)
        await scenario_timeout(tmp)
        await scenario_unknown_token(tmp)
        await scenario_double_click(tmp)
        await scenario_run_ends_first(tmp)
        await scenario_session_mapping(tmp)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
