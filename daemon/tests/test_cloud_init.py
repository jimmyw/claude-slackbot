"""Assert the security invariants of the rendered cloud-init.

These are properties a careless YAML edit could quietly undo, and the cost of
noticing late is a rebuilt VM with a weaker guest than the one it replaced. Two
of them were already broken once:

  * the agent account had a full-shell key alongside its forced command, so the
    human's interactive session shared the forwarded ssh-agent with the agent and
    could read its Claude token;
  * the gate's own files were agent-owned, so one approved Write disabled the
    approval gate for every future session.

Run:  .venv/bin/python -m tests.test_cloud_init
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


def render() -> dict:
    key = Path("~/.ssh/agent_vm_ed25519.pub").expanduser()
    admin = Path("~/.ssh/agent_vm_admin_ed25519.pub").expanduser()
    if not key.is_file() or not admin.is_file():
        print("  (skipped: provisioning keys not present on this host)")
        sys.exit(0)
    out = subprocess.run(
        [sys.executable, str(REPO / "bootstrap/render-cloud-init.py"),
         str(REPO), str(key), str(admin)],
        capture_output=True, text=True, check=True,
    )
    return yaml.safe_load(out.stdout)


def main() -> int:
    d = render()
    users = {u["name"]: u for u in d["users"]}

    print("\n[1] the agent account is confined")
    agent = users.get("agent")
    check("an agent user exists", agent is not None)
    if agent:
        keys = agent.get("ssh_authorized_keys") or []
        check("agent has exactly one key", len(keys) == 1, keys)
        check("agent's key is pinned to the forced command",
              all(k.startswith('command="/usr/local/bin/agent-exec"') for k in keys),
              keys)
        check("NO full-shell key on the agent account",
              not any(k.startswith("ssh-") for k in keys), keys)
        check("agent has no sudo", not agent.get("sudo"), agent.get("sudo"))
        check("agent cannot be logged into by password",
              agent.get("lock_passwd") is True)

    print("\n[2] the admin account is the human's route in")
    admin = users.get("admin")
    check("an admin user exists", admin is not None)
    if admin:
        keys = admin.get("ssh_authorized_keys") or []
        check("admin has sudo", bool(admin.get("sudo")))
        check("admin keys are plain (not forced-command)",
              all(k.startswith("ssh-") for k in keys), keys)
        check("admin has at least the provisioning key", len(keys) >= 1, len(keys))

        operator = REPO / "bootstrap/operator-keys.pub"
        if operator.is_file():
            wanted = [
                line.strip() for line in operator.read_text().splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            for w in wanted:
                comment = w.split()[-1]
                check(f"operator key '{comment}' is on admin",
                      any(w == k for k in keys), comment)
                # The whole point of the earlier tightening.
                agent_keys = (users.get("agent") or {}).get("ssh_authorized_keys") or []
                check(f"operator key '{comment}' is NOT on agent",
                      not any(w in k for k in agent_keys), comment)

    print("\n[3] the gate's files are root-owned in the payload")
    installs = [
        " ".join(c) for c in d.get("runcmd", []) if isinstance(c, list)
    ]
    for path in ["/etc/claude-agent/approve.py", "/etc/claude-agent/settings.json",
                 "/usr/local/bin/agent-exec", "/home/agent/CLAUDE.md"]:
        line = next((i for i in installs if i.endswith(path)), None)
        check(f"{path} installed root-owned",
              line is not None and "-o root" in line, line)

    memory = next((i for i in installs if i.endswith("/home/agent/memory/MEMORY.md")), None)
    check("MEMORY.md installed agent-owned (the agent must write it)",
          memory is not None and "-o agent" in memory, memory)

    print("\n[4] nothing secret is in the rendered output")
    text = yaml.dump(d)
    for marker in ["PRIVATE KEY", "xoxb-", "xapp-", "sk-ant-"]:
        check(f"no '{marker}' in the seed", marker not in text)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
