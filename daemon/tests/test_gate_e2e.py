"""End-to-end test of the approval gate against a real Claude Code run.

This is the one test that proves the whole mechanism, not just its parts:

    agent-exec  ->  claude -p  ->  PreToolUse hook  ->  approval listener
                                                             |
                                     Slack button (auto-clicked here)
                                                             |
                                        verdict  ->  hook  ->  tool runs or not

Only two things are stubbed: SSH (agent-exec runs locally instead of in the VM)
and the human clicking the button. Everything else is the real path, including a
real Claude Code process and the real hook script.

Needs working Claude Code auth and spends a small amount of API usage.

Run:  .venv/bin/python -m tests.test_gate_e2e
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from slackagent.approvals import ACTION_APPROVE, ACTION_DENY, ApprovalService
from slackagent.config import Config
from slackagent.store import Store

REPO = Path(__file__).resolve().parents[2]
AGENT_EXEC = REPO / "vm-files/usr/local/bin/agent-exec"
HOOK = REPO / "vm-files/home/agent/.claude/hooks/approve.py"
AUTHORIZED = "U_JIMMY"

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


class AutoClickingSlack:
    """A Slack that clicks the button for us, the way Jimmy would."""

    def __init__(self, service_getter, decision: str) -> None:
        self._service_getter = service_getter
        self._decision = decision
        self.posted: list[dict] = []
        self.updated: list[dict] = []
        self.clicked: list[str] = []
        self._counter = 0

    async def chat_postMessage(self, **kwargs):  # noqa: N802
        self._counter += 1
        ts = f"1700000000.{self._counter:06d}"
        self.posted.append({**kwargs, "ts": ts})

        approval_id = None
        for block in kwargs.get("blocks") or []:
            if block.get("type") == "actions":
                approval_id = block["elements"][0]["value"]

        if approval_id:
            asyncio.create_task(self._click(approval_id))
        return {"ts": ts, "ok": True}

    async def chat_update(self, **kwargs):  # noqa: N802
        self.updated.append(kwargs)
        return {"ok": True}

    async def _click(self, approval_id: str) -> None:
        await asyncio.sleep(0.2)
        self.clicked.append(approval_id)
        action_id = ACTION_APPROVE if self._decision == "approve" else ACTION_DENY

        async def respond(_payload):
            return None

        await self._service_getter().handle_button(
            {"user": {"id": AUTHORIZED}},
            {"action_id": action_id, "value": approval_id},
            respond,
        )


def make_config(db: Path, port: int) -> Config:
    key = db.parent / "fake_key"
    key.write_text("placeholder")
    return Config(
        bot_token="xoxb-fake",
        app_token="xapp-fake",
        authorized_user=AUTHORIZED,
        vm_host="127.0.0.1",
        vm_user="agent",
        vm_ssh_key=key,
        vm_domain="agent-vm",
        vm_workdir=str(db.parent),
        libvirt_uri="qemu:///system",
        forward_agent=False,
        approval_host="127.0.0.1",
        approval_port=port,
        approval_timeout_s=120,
        tunnel_port_low=9101,
        tunnel_port_high=9199,
        db_path=db,
        update_interval_s=0.0,
    )


async def run_agent_exec(
    workdir: Path, settings: Path, prompt: str, port: int, run_token: str
) -> tuple[int, list[dict], str]:
    """Invoke agent-exec exactly as the daemon does, minus the SSH hop."""
    job = json.dumps(
        {
            "prompt": prompt,
            "session_id": "11111111-2222-3333-4444-555555555555",
            "resume": False,
            "run_token": run_token,
            "approval_port": port,
            "cwd": str(workdir),
        }
    ).encode()

    process = await asyncio.create_subprocess_exec(
        "bash",
        str(AGENT_EXEC),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            "PATH": f"/home/tibber/.local/bin:/usr/bin:/bin",
            "HOME": str(Path.home()),
            "AGENT_SETTINGS": str(settings),
        },
    )
    out, err = await asyncio.wait_for(process.communicate(job), timeout=300)

    events = []
    for line in out.decode(errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return process.returncode or 0, events, err.decode(errors="replace")


def write_settings(directory: Path) -> Path:
    """The guest settings.json, with the hook path rewritten for local running."""
    real = json.loads(
        (REPO / "vm-files/home/agent/.claude/settings.json").read_text()
    )
    hooks = real["hooks"]["PreToolUse"][0]["hooks"][0]
    hooks["command"] = f"python3 {HOOK}"
    path = directory / "settings.json"
    path.write_text(json.dumps(real))
    return path


async def scenario(decision: str, port: int, tmp: Path) -> None:
    print(f"\n[{decision}] real claude run, operator clicks {decision.title()}")
    workdir = tmp / decision
    workdir.mkdir()
    settings = write_settings(workdir)
    canary = workdir / "canary.txt"

    store = Store(workdir / "state.sqlite3")
    holder: dict[str, ApprovalService] = {}
    slack = AutoClickingSlack(lambda: holder["svc"], decision)
    config = make_config(workdir / "state.sqlite3", port)
    service = ApprovalService(config, store, slack)
    holder["svc"] = service
    await service.start()

    try:
        run_token = f"tok-{decision}"
        service.register_run(run_token, "C1", "1.1", "sess")

        code, events, stderr = await run_agent_exec(
            workdir,
            settings,
            f"Create the file {canary} containing exactly the word HELLO. "
            "Use the Write tool. If a tool call is blocked, stop immediately and "
            "say so — do not attempt any alternative approach.",
            port,
            run_token,
        )

        check("agent-exec exited cleanly", code == 0, f"exit={code} stderr={stderr[-300:]}")
        check("the hook asked for approval", len(slack.posted) >= 1, slack.posted)
        check("a button was clicked", len(slack.clicked) >= 1)

        posted_tools = [
            p["text"] for p in slack.posted if "Approval needed" in p.get("text", "")
        ]
        check("the approval names the Write tool",
              any("Write" in t for t in posted_tools), posted_tools)

        result = next((e for e in events if e.get("type") == "result"), None)
        check("a result event came back", result is not None)

        if decision == "approve":
            check("the file was created", canary.exists())
            check("with the right contents",
                  canary.exists() and "HELLO" in canary.read_text(),
                  canary.read_text()[:80] if canary.exists() else "<no file>")
            check("no denial recorded in the result",
                  result is not None and not result.get("permission_denials"),
                  (result or {}).get("permission_denials"))
        else:
            check("the file was NOT created", not canary.exists())
            denials = (result or {}).get("permission_denials") or []
            check("the result records the denial", len(denials) >= 1, denials)
            check("the denial was the Write",
                  any(d.get("tool_name") == "Write" for d in denials), denials)

        rows = [row for row in (store.approval(a) for a in slack.clicked) if row]
        expected_state = "approved" if decision == "approve" else "denied"
        check(f"audit log says {expected_state}",
              len(rows) >= 1 and all(r["state"] == expected_state for r in rows),
              [r["state"] for r in rows])
    finally:
        await service.stop()
        store.close()


async def main() -> int:
    if not AGENT_EXEC.is_file():
        print(f"missing {AGENT_EXEC}")
        return 1

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        await scenario("approve", 19201, tmp)
        await scenario("deny", 19202, tmp)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
