"""`|resume` — start answering in this thread again."""
from __future__ import annotations

import argparse
import datetime

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
    paused = {
        (channel, thread): (at, by)
        for channel, thread, at, by in ctx.store.list_paused()
    }
    entry = paused.get((ctx.channel, ctx.thread_ts))

    if not ctx.store.resume_thread(ctx.channel, ctx.thread_ts):
        elsewhere = len(paused)
        note = (
            f" {elsewhere} other thread{'s' if elsewhere != 1 else ''} "
            f"{'are' if elsewhere != 1 else 'is'} paused; a pause only ever "
            "covers the thread it was set in."
            if elsewhere else ""
        )
        await ctx.say(f"This thread was not paused, so nothing changed.{note}")
        return

    since = ""
    if entry:
        when = datetime.datetime.fromtimestamp(entry[0]).strftime("%Y-%m-%d %H:%M")
        who = f" by <@{entry[1]}>" if entry[1] else ""
        since = f" Paused{who} at {when}."
    await ctx.say(
        f":white_check_mark: Answering here again.{since} Anything said while "
        "paused was not seen — say it again if it still matters."
    )
