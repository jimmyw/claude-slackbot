"""`|help` — list every registered command."""
from __future__ import annotations

import argparse

from . import COMMAND_PREFIX, Context, SlackParser

NAME = "help"
ALIASES = ("commands", "?")
SUMMARY = "list these commands"


def build_parser() -> argparse.ArgumentParser:
    return SlackParser(
        prog=f"{COMMAND_PREFIX}{NAME}",
        description="List the local commands the daemon answers itself.",
        epilog=f"Use {COMMAND_PREFIX}<command> -h for a command's own options.",
    )


async def run(ctx: Context, args: argparse.Namespace) -> None:
    # Imported here rather than at module scope: the registry imports this module,
    # so a top-level import of it would be circular.
    from . import commands

    lines = [
        f"*Local commands* — messages starting with `{COMMAND_PREFIX}`. "
        "I answer these myself; they never reach Claude."
    ]
    # Pad outside the backticks: trailing spaces inside them render as part of the
    # code span.
    width = max(len(c.name) for c in commands())
    for command in commands():
        alias = f"  (also {', '.join(command.aliases)})" if command.aliases else ""
        name = f"`{COMMAND_PREFIX}{command.name}`"
        lines.append(f"  {name}{' ' * (width - len(command.name))}  "
                     f"{command.summary}{alias}")
    lines.append(
        f"\nDetail for one: `{COMMAND_PREFIX}grants -h`, "
        f"`{COMMAND_PREFIX}revoke -h`."
    )
    lines.append(
        f"Anything not starting with `{COMMAND_PREFIX}` is a request for Claude, "
        "so `revoke the old deploy key` reaches it normally."
    )
    await ctx.say("\n".join(lines))
