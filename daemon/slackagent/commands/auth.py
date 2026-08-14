"""`|auth` — switch how much the agent may do without asking."""
from __future__ import annotations

import argparse
import datetime

from . import COMMAND_PREFIX, Context, SlackParser

NAME = "auth"
ALIASES = ("mode", "policy")
SUMMARY = "show or change how much runs without approval"

SETTING = "agent_policy"

# Ordered most open to most closed, which is the order |auth lists them in.
_MODES = {
    "open": (
        "*Nothing is ever asked.* The gate is off.\n"
        "    What still protects the VM is the operating system, not this switch: "
        "the agent has no sudo, and the hook, its settings and `agent-exec` are "
        "root-owned, so it cannot escalate or disable its own gate even here.\n"
        "    What this newly permits: `git push` using the forwarded ssh-agent — "
        "with a personal key that means anything you can write — and edits to its "
        "own `~/.gitconfig`, `~/.claude` and `~/.ssh`, which persist between runs."
    ),
    "permissive": (
        "Ordinary work runs: reading, writing inside `/home/agent/work`, "
        "building, testing, git, curl, package installs.\n"
        "    Asks for: escalation and machine changes (`sudo`, `systemctl`, "
        "`apt`, `mount`, `dd`, `nft`, `crontab`); state outside the VM "
        "(`git push`, `git remote set-url`, `git config --global`); writes "
        "outside the workspace; and its own dotfiles."
    ),
    "strict": (
        "Every `Bash` call asks, whatever it is.\n"
        "    Reading, and writing inside the workspace, still run without asking — "
        "those are not part of this switch."
    ),
}

_DESCRIPTION = """\
Show or change the approval policy.

  open        nothing is ever asked; the gate is off
  permissive  ordinary shell work runs; escalation, pushes and anything reaching
              outside /home/agent/work still ask
  strict      every Bash call asks

The change applies to the NEXT message. A run already in flight keeps the policy
it started with, because the policy travels with the job.

The policy itself is enforced by a root-owned hook inside the VM, so the agent
cannot alter it whichever mode is selected. This only chooses the mode.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = SlackParser(
        prog=f"{COMMAND_PREFIX}{NAME}",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f"Examples:  {COMMAND_PREFIX}auth            show the current mode\n"
            f"           {COMMAND_PREFIX}auth strict     ask for every command\n"
            f"           {COMMAND_PREFIX}auth permissive back to the default"
        ),
    )
    parser.add_argument(
        "mode", nargs="?", choices=sorted(_MODES),
        help="the mode to switch to; omit to show the current one",
    )
    return parser


async def run(ctx: Context, args: argparse.Namespace) -> None:
    current = ctx.store.get_setting(SETTING, ctx.config.agent_policy)

    if args.mode is None:
        meta = ctx.store.setting_meta(SETTING)
        if meta is None:
            origin = "the default from `daemon/.env`"
        else:
            when = datetime.datetime.fromtimestamp(meta[0]).strftime("%Y-%m-%d %H:%M")
            origin = f"set by <@{meta[1]}> at {when}" if meta[1] else f"set at {when}"

        lines = [f"Policy: *{current}* — {origin}", ""]
        lines.append("*All modes*, most open first:")
        for name, description in _MODES.items():
            marker = " ← current" if name == current else ""
            lines.append(f"\n  `{name}`{marker}\n    {description}")
        others = [m for m in _MODES if m != current]
        lines.append(
            "\nSwitch with "
            + ", ".join(f"`{COMMAND_PREFIX}auth {m}`" for m in others)
            + "."
        )
        await ctx.say("\n".join(lines))
        return

    if args.mode == current:
        await ctx.say(f"Policy is already *{current}*. Nothing changed.")
        return

    ctx.store.set_setting(SETTING, args.mode, ctx.user)
    warning = ""
    if args.mode == "open":
        # Worth one line at the moment of choosing, not buried in a doc.
        warning = (
            "\n\n:warning: Nothing will ask from now on, including `git push` "
            "with your forwarded key. `"
            f"{COMMAND_PREFIX}auth permissive` puts the gate back."
        )
    await ctx.say(
        f"Policy is now *{args.mode}*, from your next message.\n\n"
        f"{_MODES[args.mode]}{warning}"
    )
