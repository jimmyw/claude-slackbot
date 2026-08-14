"""`|revoke` — remove standing grants."""
from __future__ import annotations

import argparse

from . import COMMAND_PREFIX, CommandError, Context, SlackParser

NAME = "revoke"
ALIASES = ()
SUMMARY = "remove a standing grant, or all of them"


def _target(value: str) -> str:
    """A grant id or the word 'all'.

    Validated here so argparse produces the usage message, rather than the command
    accepting anything and explaining itself afterwards.
    """
    lowered = value.lower()
    if lowered in {"all", "*"}:
        return "all"
    if value.isdigit():
        return value
    raise argparse.ArgumentTypeError(
        f"expected a grant id or 'all', got {value!r}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = SlackParser(
        prog=f"{COMMAND_PREFIX}{NAME}",
        description=(
            "Remove a standing grant so its tool calls ask for approval again."
        ),
        epilog=(
            f"Examples:  {COMMAND_PREFIX}revoke 3   "
            f"{COMMAND_PREFIX}revoke all\n"
            f"List ids with {COMMAND_PREFIX}grants."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "target", type=_target,
        help="the grant id from |grants, or 'all' to remove every grant",
    )
    return parser


async def run(ctx: Context, args: argparse.Namespace) -> None:
    # Belt and braces: the dispatcher checks this too, but a command that changes
    # what runs unattended should not depend on its caller for that.
    if not ctx.is_operator:
        raise CommandError(
            f"Only <@{ctx.config.authorized_user}> can change grants."
        )

    if args.target == "all":
        removed = ctx.store.revoke_all()
        await ctx.say(
            f"Revoked {removed} grant(s). Everything asks again."
            if removed
            else "There were no grants to revoke."
        )
        return

    if ctx.store.revoke_grant(int(args.target)):
        await ctx.say(f"Revoked grant `{args.target}`.")
    else:
        await ctx.say(
            f"No grant `{args.target}`. "
            f"Use `{COMMAND_PREFIX}grants` to list them."
        )
