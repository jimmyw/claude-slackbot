"""`|pending` — show approvals still waiting, and put their buttons back."""
from __future__ import annotations

import argparse
import json

from . import COMMAND_PREFIX, Context, SlackParser

NAME = "pending"
ALIASES = ("waiting",)
SUMMARY = "list waiting approvals, or repost their buttons"

_DESCRIPTION = """\
List the approvals still blocking a run, and optionally post fresh buttons.

Needed because an approval message can be lost: a reply to a Slack button's
response_url replaces the original message by default, so a click from someone who
is not the approver used to delete the buttons and leave the request unanswerable.
That is fixed, but a message can still be deleted by hand or lost in a busy thread.

Only approvals with a live waiter are listed. A waiter exists while the hook in the
VM is holding its request open, so after a timeout or a daemon restart there is
nothing left to answer and new buttons would do nothing.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = SlackParser(
        prog=f"{COMMAND_PREFIX}{NAME}",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f"Examples:  {COMMAND_PREFIX}pending           list them\n"
            f"           {COMMAND_PREFIX}pending --repost  post their buttons again"
        ),
    )
    parser.add_argument(
        "--repost", action="store_true",
        help="post fresh Approve/Deny buttons for each waiting approval",
    )
    return parser


def _describe(item: dict) -> str:
    try:
        data = json.loads(item["tool_input_json"] or "null")
    except (json.JSONDecodeError, ValueError):
        data = None
    detail = ""
    if isinstance(data, dict):
        for key in ("command", "file_path", "url", "pattern"):
            value = data.get(key)
            if isinstance(value, str) and value:
                detail = f" — `{value[:90]}`"
                break
    who = f" (requested by <@{item['requested_by']}>)" if item["requested_by"] else ""
    return f"  {item['tool_name']}{detail}{who}"


async def run(ctx: Context, args: argparse.Namespace) -> None:
    waiting = ctx.approvals.pending()

    if not waiting:
        await ctx.say(
            "Nothing is waiting for approval.\n"
            "If a request seemed to hang, it has already timed out and been "
            "denied — ask the agent to try again."
        )
        return

    lines = [f"*{len(waiting)} approval(s) waiting:*"]
    lines.extend(_describe(item) for item in waiting)

    if not args.repost:
        lines.append(
            f"\n`{COMMAND_PREFIX}pending --repost` to post their buttons again."
        )
        await ctx.say("\n".join(lines))
        return

    await ctx.say("\n".join(lines))
    reposted = 0
    for item in waiting:
        if await ctx.approvals.repost(item["id"]):
            reposted += 1
    await ctx.say(
        f"Reposted {reposted} of {len(waiting)}."
        if reposted
        else "None could be reposted; they must have just been answered."
    )
