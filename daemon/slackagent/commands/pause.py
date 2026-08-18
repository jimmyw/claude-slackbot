"""`|pause` — stop Claude answering in this thread, without leaving it."""
from __future__ import annotations

import argparse

from ..store import MODE_PAUSED, MODE_SILENT
from . import COMMAND_PREFIX, Context, SlackParser

NAME = "pause"
ALIASES = ("mute",)
SUMMARY = "stop answering in this thread entirely"

_DESCRIPTION = f"""\
Pause the agent in the thread you type this in.

While a thread is paused NOTHING in it reaches Claude — not a reply, not a
mention, not a direct message. The daemon posts nothing either: a paused thread is
silent, not answered with an apology.

The pause is stored in the daemon's database, so it survives a restart and lasts
until someone runs `{COMMAND_PREFIX}resume` in the same thread. The thread's
session is untouched: resuming continues the same conversation with its context
intact.

Approvals already on screen are not affected — a run in flight keeps going, and
its buttons still work. Pause stops new messages starting new turns.

If you want it to stay reachable, `|silent` answers when tagged and ignores
everything else. Pause is the stronger of the two: a tag does not wake it.
"""


def build_parser() -> argparse.ArgumentParser:
    return SlackParser(
        prog=f"{COMMAND_PREFIX}{NAME}",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


async def run(ctx: Context, args: argparse.Namespace) -> None:
    previous = ctx.store.set_thread_mode(
        ctx.channel, ctx.thread_ts, MODE_PAUSED, ctx.user
    )
    if previous == MODE_PAUSED:
        await ctx.say(
            f"Already paused here. `{COMMAND_PREFIX}resume` to start answering "
            "again."
        )
        return

    if previous == MODE_SILENT:
        await ctx.say(
            ":no_bell: Fully muted now — this thread was mention-only, so tagging me "
            f"used to work and no longer does. `{COMMAND_PREFIX}resume` to lift it."
        )
        return

    # Worth saying when it happens: pausing a thread the bot has never run in is
    # legitimate (it stops it being drawn in by a later mention), but it is also
    # what a mistyped pause looks like.
    unknown = ctx.store.find_session(ctx.channel, ctx.thread_ts) is None
    note = (
        "\nI have no session in this thread yet, so this is a pre-emptive pause: "
        "a mention here will now be ignored too."
        if unknown else ""
    )
    await ctx.say(
        ":large_orange_circle: Paused in this thread. Nothing said here reaches "
        f"Claude — mentions included — until `{COMMAND_PREFIX}resume`. Other "
        f"threads are unaffected.{note}"
    )
