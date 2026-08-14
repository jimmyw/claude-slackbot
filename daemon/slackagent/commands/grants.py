"""`|grants` — list the standing "always allow" grants."""
from __future__ import annotations

import argparse

from . import COMMAND_PREFIX, Context, SlackParser

NAME = "grants"
ALIASES = ("grant",)
SUMMARY = "list standing grants and their use counts"

_DESCRIPTION = """\
List the standing grants. A grant lets matching tool calls run without a button.

Grants are created by pressing "Always allow" on an approval, and are stored on
the host, so the agent can never grant itself anything.

Match types:
  prefix   a command and optional subcommand: `git status` covers
           `git status --short`, but never `git statusfoo`
  exact    the whole command, byte for byte. Used where a prefix would be unsafe:
           redirection, substitution, interpreters, destructive commands
  any      the tool name is the whole scope. For tools with nothing to scope by,
           such as ToolSearch and mcp__* tools
"""


def build_parser() -> argparse.ArgumentParser:
    parser = SlackParser(
        prog=f"{COMMAND_PREFIX}{NAME}",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Remove one with {COMMAND_PREFIX}revoke <id>.",
    )
    parser.add_argument(
        "--tool", metavar="NAME",
        help="only grants for this tool, e.g. --tool Bash",
    )
    parser.add_argument(
        "--unused", action="store_true",
        help="only grants that have never matched anything",
    )
    return parser


async def run(ctx: Context, args: argparse.Namespace) -> None:
    grants = ctx.store.list_grants()
    if args.tool:
        wanted = args.tool.lower()
        grants = [g for g in grants if g.tool_name.lower() == wanted]
    if args.unused:
        grants = [g for g in grants if g.use_count == 0]

    if not grants:
        scope = ""
        if args.tool:
            scope = f" for `{args.tool}`"
        if args.unused:
            scope += " that are unused"
        await ctx.say(
            f"No standing grants{scope}. Every gated tool call asks.\n"
            'Press *Always allow* on an approval to add one.'
        )
        return

    lines = [f"*{len(grants)} standing grant(s)* — these skip the button entirely:"]
    for g in grants:
        if g.match_type == "any":
            scope = "any use"
        elif g.match_type == "exact":
            scope = f"exactly `{g.pattern}`"
        else:
            scope = f"`{g.pattern}` and below"
        used = f"{g.use_count} use{'s' if g.use_count != 1 else ''}"
        lines.append(f"  `{g.id}`  {g.tool_name}: {scope}  ({used})")
    lines.append(
        f"`{COMMAND_PREFIX}revoke <id>` to remove one, "
        f"`{COMMAND_PREFIX}revoke all` to clear them."
    )
    await ctx.say("\n".join(lines))
