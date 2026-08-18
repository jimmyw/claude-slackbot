"""`|resume` — start answering in this thread again."""
from __future__ import annotations

import argparse
import datetime

from ..store import MODE_ACTIVE, MODE_PAUSED
from . import COMMAND_PREFIX, Context, SlackParser

NAME = "resume"
ALIASES = ("unmute",)
SUMMARY = "start answering in this thread again"

_DESCRIPTION = f"""\
Lift a `{COMMAND_PREFIX}pause` in the thread you type this in.

The thread's Claude session was never closed, so the next message continues the
same conversation with its context intact — nothing was lost while it was quiet,
but nothing said during the pause was seen either.
"""


def build_parser() -> argparse.ArgumentParser:
    return SlackParser(
        prog=f"{COMMAND_PREFIX}{NAME}",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


async def run(ctx: Context, args: argparse.Namespace) -> None:
    # Read the row before clearing it: the confirmation names who muted the thread
    # and when, which is gone the moment the row is deleted.
    entry = ctx.store.thread_mode_entry(ctx.channel, ctx.thread_ts)
    previous = ctx.store.set_thread_mode(
        ctx.channel, ctx.thread_ts, MODE_ACTIVE, ctx.user
    )

    if previous == MODE_ACTIVE:
        elsewhere = len(ctx.store.list_thread_modes())
        note = (
            f" {elsewhere} other thread{'s' if elsewhere != 1 else ''} "
            f"{'are' if elsewhere != 1 else 'is'} paused; a pause only ever "
            "covers the thread it was set in."
            if elsewhere else ""
        )
        await ctx.say(f"This thread was not paused, so nothing changed.{note}")
        return

    since = ""
    if entry is not None:
        when = datetime.datetime.fromtimestamp(entry.set_at).strftime("%Y-%m-%d %H:%M")
        who = f" by <@{entry.set_by}>" if entry.set_by else ""
        dropped = (
            f" {entry.dropped} message{'s' if entry.dropped != 1 else ''} went "
            "unseen." if entry.dropped else ""
        )
        since = f" Paused{who} at {when}.{dropped}"
    await ctx.say(
        f":white_check_mark: Answering here again.{since} Anything said while "
        "paused was not seen — say it again if it still matters."
    )
