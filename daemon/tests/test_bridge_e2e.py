"""End-to-end test against the live VM: the whole chain, nothing stubbed but Slack.

    daemon Bridge ──ssh -R──▶ VM agent-exec ──▶ claude -p
                                                    │
                                          PreToolUse hook
                                                    │
                                    POST over the reverse tunnel
                                                    │
                              daemon ApprovalService ──▶ (button auto-clicked)
                                                    │
                                        verdict ──▶ hook ──▶ tool runs or not

This is the one test that proves the reverse tunnel, the forced command, the
root-owned gate, the guest's token, and the approval round-trip all work
*together*. test_gate_e2e.py proves the gate mechanism locally; this proves the
transport carrying it.

Needs a provisioned, authenticated VM. Spends real API usage (two runs).

Run:  .venv/bin/python -m tests.test_bridge_e2e <vm-ip>
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

from slackagent.approvals import ACTION_APPROVE, ACTION_DENY, ApprovalService
from slackagent.bridge import Bridge
from slackagent.config import Config
from slackagent.store import Store

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


class AutoClickingSlack:
    """Stands in for Slack and for the human pressing the button."""

    def __init__(self, service_getter, decision: str) -> None:
        self._get = service_getter
        self._decision = decision
        self.posted: list[dict] = []
        self.updated: list[dict] = []
        self.clicked: list[str] = []
        self._n = 0

    async def chat_postMessage(self, **kwargs):  # noqa: N802
        self._n += 1
        ts = f"1700000000.{self._n:06d}"
        self.posted.append({**kwargs, "ts": ts})
        for block in kwargs.get("blocks") or []:
            if block.get("type") == "actions":
                asyncio.create_task(self._click(block["elements"][0]["value"]))
        return {"ts": ts, "ok": True}

    async def chat_update(self, **kwargs):  # noqa: N802
        self.updated.append(kwargs)
        return {"ok": True}

    async def _click(self, approval_id: str) -> None:
        await asyncio.sleep(0.3)
        self.clicked.append(approval_id)

        async def respond(_):
            return None

        await self._get().handle_button(
            {"user": {"id": "U_JIMMY"}},
            {
                "action_id": ACTION_APPROVE if self._decision == "approve" else ACTION_DENY,
                "value": approval_id,
            },
            respond,
        )


def make_config(vm_host: str, db: Path, port: int) -> Config:
    return Config(
        bot_token="xoxb-fake",
        app_token="xapp-fake",
        authorized_user="U_JIMMY",
        vm_host=vm_host,
        vm_user="agent",
        vm_ssh_key=Path("~/.ssh/agent_vm_ed25519").expanduser(),
        vm_domain="agent-vm",
        vm_workdir="/home/agent/work",
        libvirt_uri="qemu:///system",
        approval_host="127.0.0.1",
        approval_port=port,
        approval_timeout_s=120,
        tunnel_port_low=9150,
        tunnel_port_high=9179,
        db_path=db,
        update_interval_s=0.0,
    )


async def scenario(vm_host: str, decision: str, port: int, tmp: Path) -> None:
    print(f"\n[{decision}] real VM, real tunnel, operator clicks {decision.title()}")

    canary = f"/home/agent/work/canary-{decision}-{uuid.uuid4().hex[:8]}.txt"
    store = Store(tmp / f"{decision}.sqlite3")
    holder: dict[str, ApprovalService] = {}
    slack = AutoClickingSlack(lambda: holder["svc"], decision)
    config = make_config(vm_host, tmp / f"{decision}.sqlite3", port)
    service = ApprovalService(config, store, slack)
    holder["svc"] = service
    await service.start()

    bridge = Bridge(config)
    run_token = uuid.uuid4().hex
    session_id = str(uuid.uuid4())
    service.register_run(run_token, "C1", "1.1", session_id)

    events: list[dict] = []
    try:
        async for event in bridge.run(
            prompt=(
                f"Create the file {canary} containing exactly the word HELLO. "
                "Use the Write tool. If a tool call is blocked, stop immediately "
                "and say so — do not attempt any alternative approach."
            ),
            session_id=session_id,
            resume=False,
            run_token=run_token,
        ):
            events.append(event)
    finally:
        service.unregister_run(run_token)

    init = next(
        (e for e in events if e.get("type") == "system" and e.get("subtype") == "init"),
        None,
    )
    result = next((e for e in events if e.get("type") == "result"), None)
    transport = next((e for e in events if e.get("type") == "_bridge"), None)

    check("the bridge reached the CLI", init is not None)
    check("the session id round-tripped",
          init is not None and init.get("session_id") == session_id)
    check("transport exited cleanly",
          transport is not None and transport.get("exit_code") == 0,
          (transport or {}).get("stderr", "")[-200:])
    check("a result came back", result is not None)
    check("the run authenticated",
          result is not None and "Not logged in" not in (result.get("result") or ""),
          (result or {}).get("result", "")[:80])

    # The point of the whole exercise: the hook reached the daemon over the
    # reverse tunnel and blocked until a human answered.
    check("the gate fired over the reverse tunnel", len(slack.posted) >= 1)
    check("the approval names the Write tool",
          any("Write" in p.get("text", "") for p in slack.posted),
          [p.get("text") for p in slack.posted])
    check("a button was clicked", len(slack.clicked) >= 1)

    exists = await file_exists(config, canary)
    if decision == "approve":
        check("the file WAS created in the VM", exists)
    else:
        check("the file was NOT created in the VM", not exists)
        denials = (result or {}).get("permission_denials") or []
        check("the result records the denial", len(denials) >= 1, denials)

    rows = [r for r in (store.approval(a) for a in slack.clicked) if r]
    want = "approved" if decision == "approve" else "denied"
    check(f"audit log says {want}",
          len(rows) >= 1 and all(r["state"] == want for r in rows),
          [r["state"] for r in rows])

    await service.stop()
    store.close()


async def file_exists(config: Config, path: str) -> bool:
    """Check via the admin key — the daemon key cannot run arbitrary commands."""
    admin = Path("~/.ssh/agent_vm_admin_ed25519").expanduser()
    process = await asyncio.create_subprocess_exec(
        "ssh", "-i", str(admin),
        "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        f"admin@{config.vm_host}",
        f"sudo test -f {path}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await process.wait() == 0


async def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 64
    vm_host = sys.argv[1]

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        await scenario(vm_host, "approve", 19301, tmp)
        await scenario(vm_host, "deny", 19302, tmp)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
