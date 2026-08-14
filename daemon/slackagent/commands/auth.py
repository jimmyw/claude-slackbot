"""`|auth` — switch how much the agent may do without asking."""
from __future__ import annotations

import argparse
import datetime

from . import COMMAND_PREFIX, Context, SlackParser

NAME = "auth"
ALIASES = ("mode", "policy")
SUMMARY = "show or change how much runs without approval"

SETTING = "agent_policy"

_MODES = {
    "permissive": (
        "Reading, writing inside the workspace, and ordinary shell work — build, "
        "test, git, curl, package installs — all run without asking.\n"
        "  Still asks for: escalation and machine changes (`sudo`, `systemctl`, "
        "`apt`, `mount`, `dd`, `nft`, `crontab`); state outside the VM "
        "(`git push`, `git remote set-url`, `git config --global`); writes "
        "outside `/home/agent/work`; and its own dotfiles (`~/.ssh`, "
        "`~/.gitconfig`, `~/.claude`)."
    ),
    "strict": (
        "Every `Bash` call asks, whatever it is. Reading and writing inside the "
        "workspace still run without asking — those are not part of this switch."
    ),
}

_DESCRIPTION = """\
Show or change the approval policy.

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
        other = "strict" if current == "permissive" else "permissive"
        await ctx.say(
            f"Policy: *{current}* — {origin}\n\n{_MODES[current]}\n\n"
            f"Switch with `{COMMAND_PREFIX}auth {other}`."
        )
        return

    if args.mode == current:
        await ctx.say(f"Policy is already *{current}*. Nothing changed.")
        return

    ctx.store.set_setting(SETTING, args.mode, ctx.user)
    await ctx.say(
        f"Policy is now *{args.mode}*, from your next message.\n\n"
        f"{_MODES[args.mode]}"
    )
