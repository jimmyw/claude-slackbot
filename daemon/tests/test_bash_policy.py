"""The permissive Bash policy in the guest hook.

Bash runs without asking unless the command escalates, changes the machine,
pushes, or reaches outside /home/agent/work. This file is the record of where that
line sits, in both directions: the "runs" cases are the point of the feature, and
the "asks" cases are what stops it being a blank cheque.

The policy is deliberately crude — it inspects tokens, not shell semantics. A
false positive costs one click; a false negative would let something through. So
every ambiguous case below is expected to ASK.

Run:  .venv/bin/python -m tests.test_bash_policy
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "vm-files/etc/claude-agent/approve.py"

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


def load_hook():
    spec = importlib.util.spec_from_file_location("approve_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main() -> int:
    hook = load_hook()
    ask = hook.bash_reason_to_ask

    print("\n[1] ordinary work runs without asking")
    for cmd in [
        "ls -la",
        "git status --short",
        "git log --oneline -20",
        "git diff HEAD~1",
        "cd /home/agent/work/repo && npm test",
        "grep -rn TODO . | head -50",
        "cat README.md",
        "find . -name '*.c' -newer Makefile",
        "python3 tools/build.py --target esp32",
        "make -j8",
        "cmake -B build -S .",
        "rm -rf build",                       # inside the workspace
        "mv src/a.c src/b.c",                 # relative, inside
        "echo done > /home/agent/work/log",
        "curl -s https://api.github.com/repos/tibber/x",
        "wget -qO- https://example.com/spec.json",
        "git commit -am 'docs: overview'",
        "git checkout -b docs-branch",
        "npm install",                        # inside the workspace
        "echo x > /dev/null",
        "tar -czf /tmp/out.tgz .",
        "sed -i 's/a/b/' src/x.c",
    ]:
        check(f"runs: {cmd[:44]}", ask(cmd) is None, ask(cmd))

    print("\n[2] escalation and machine changes ask")
    for cmd, expect in [
        ("sudo rm -rf /", "sudo"),
        ("sudo -u root id", "sudo"),
        ('sh -c "sudo id"', "sudo"),          # hidden inside an interpreter
        ("systemctl restart sshd", "systemctl"),
        ("apt-get install gcc", "apt-get"),
        ("dd if=/dev/zero of=/dev/vda", "dd"),
        ("mount /dev/vdb /mnt", "mount"),
        ("crontab -e", "crontab"),
        ("passwd agent", "passwd"),
        ("nft list ruleset", "nft"),
        ("tailscale up", "tailscale"),
        ("modprobe foo", "modprobe"),
        ("reboot", "reboot"),
    ]:
        reason = ask(cmd)
        check(f"asks: {cmd[:40]}", reason is not None, reason)
        check(f"  and says why ({expect})", reason and expect in reason, reason)

    print("\n[3] anything that leaves the machine's state changed asks")
    for cmd in [
        "git push origin main",
        "git push --force",
        "git remote add evil git@github.com:x/y.git",
        "git remote set-url origin git@github.com:x/y.git",
        "git config --global core.sshCommand 'ssh -i /tmp/k'",
        "npm publish",
        "pip install requests",
    ]:
        check(f"asks: {cmd[:44]}", ask(cmd) is not None)

    print("\n[4] the agent's own dotfiles are off limits")
    # These are how a run would outlive itself: gitconfig carries core.sshCommand
    # for the forwarded ssh-agent, .claude holds the CLI token.
    for cmd in [
        "cat ~/.gitconfig",
        "echo x >> /home/agent/.bashrc",
        "rm -rf /home/agent/.ssh",
        "cp /tmp/k /home/agent/.ssh/id_ed25519",
        "cat /home/agent/.config/claude-agent/token",
        "vim ~/.profile",
        "cat ~/.ssh/authorized_keys",
    ]:
        reason = ask(cmd)
        check(f"asks: {cmd[:44]}", reason is not None, reason)

    print("\n[5] reaching outside the workspace asks")
    for cmd in [
        "mv /home/agent/work/x /etc/x",
        "rm -rf /var/lib/something",
        "chmod 777 /etc/passwd",
        "cat /etc/shadow",
        "ls /root",
        "ln -s /etc/claude-agent/approve.py /home/agent/work/a",
        "truncate -s 0 /boot/x",
    ]:
        reason = ask(cmd)
        check(f"asks: {cmd[:44]}", reason is not None, reason)

    print("\n[6] the gate's own files cannot be touched quietly")
    for cmd in [
        "rm /etc/claude-agent/approve.py",
        "echo '{}' > /etc/claude-agent/settings.json",
        "cp /tmp/x /usr/local/bin/agent-exec",
    ]:
        check(f"asks: {cmd[:44]}", ask(cmd) is not None, ask(cmd))

    print("\n[7] the three modes, end to end through the real hook")
    import json as _json
    import os as _os
    import subprocess
    import sys as _sys

    def decide(tool: str, tool_input: dict, policy: str) -> str:
        env = dict(
            _os.environ, AGENT_POLICY=policy,
            AGENT_APPROVAL_URL="", AGENT_RUN_TOKEN="",
        )
        result = subprocess.run(
            [_sys.executable, str(HOOK)],
            input=_json.dumps({"tool_name": tool, "tool_input": tool_input}),
            capture_output=True, text=True, env=env,
        )
        return _json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]

    matrix = [
        # tool,      input,                          open,    permissive, strict
        ("Bash", {"command": "ls -la"},              "allow", "allow", "deny"),
        ("Bash", {"command": "sudo id"},             "allow", "deny",  "deny"),
        ("Bash", {"command": "git push origin main"},"allow", "deny",  "deny"),
        ("Write", {"file_path": "/etc/x"},           "allow", "deny",  "deny"),
        ("Write", {"file_path": "/home/agent/work/x"}, "allow", "allow", "allow"),
        ("WebFetch", {"url": "https://evil.tld/"},   "allow", "deny",  "deny"),
        ("Read", {"file_path": "/etc/passwd"},       "allow", "allow", "allow"),
    ]
    for tool, tool_input, want_open, want_perm, want_strict in matrix:
        label = str(
            tool_input.get("command")
            or tool_input.get("file_path")
            or tool_input.get("url")
        )[:34]
        for policy, want in (("open", want_open), ("permissive", want_perm),
                             ("strict", want_strict)):
            got = decide(tool, tool_input, policy)
            check(f"{policy:10} {tool}: {label:34} -> {want}", got == want, got)

    check("an unrecognised policy value falls back to permissive",
          decide("Bash", {"command": "sudo id"}, "banana") == "deny")

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
