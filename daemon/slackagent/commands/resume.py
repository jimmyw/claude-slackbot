"""`|resume` — start answering in this thread again."""
from __future__ import annotations

import argparse
import datetime

from ..store import MODE_ACTIVE, MODE_PAUSED, MODE_SILENT
from . import COMMAND_PREFIX, Context, SlackParser

NAME = "resume"
ALIASES = ("unmute", "active")
SUMMARY = "back to normal in this thread (lifts |pause or |silent)"

_DESCRIPTION = f"""\
Put the thread you type this in back to normal, lifting either
`{COMMAND_PREFIX}pause` or `{COMMAND_PREFIX}silent`.

Normal means the agent is shown every reply in the thread and decides for itself
whether a message was meant for it, staying quiet when it was not.

The thread's Claude session was never closed, so the next message continues the same
conversation with its context intact. What was said while it was paused was never
seen by anyone and does not come back; a `{COMMAND_PREFIX}silent` stretch is
different, because a tag during it brought the missed messages along.
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
            f"{'are' if elsewhere != 1 else 'is'} muted; a mode only ever covers "
            "the thread it was set in."
            if elsewhere else ""
        )
        await ctx.say(
            f"This thread was already answering normally, so nothing changed.{note}"
        )
        return

    since = ""
    if entry is not None:
        when = datetime.datetime.fromtimestamp(entry.set_at).strftime("%Y-%m-%d %H:%M")
        who = f" by <@{entry.set_by}>" if entry.set_by else ""
        was = "Paused" if previous == MODE_PAUSED else "Mention-only"
        dropped = (
            f" {entry.dropped} message{'s' if entry.dropped != 1 else ''} "
            f"{'were' if entry.dropped != 1 else 'was'} not forwarded."
            if entry.dropped else ""
        )
        since = f" {was}{who} since {when}.{dropped}"

    # The two modes made different promises while they were on, so the sentence has
    # to branch: a pause swallowed those messages for good, mention-only did not.
    tail = (
        "Anything said while paused was not seen — say it again if it still matters."
        if previous == MODE_PAUSED
        else "You can carry on; I was shown what I missed whenever you tagged me."
    )
    await ctx.say(f":white_check_mark: Answering here normally again.{since} {tail}")
