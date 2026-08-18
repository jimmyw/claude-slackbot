"""`|silent` — answer in this thread only when actually tagged."""
from __future__ import annotations

import argparse

from ..store import MODE_PAUSED, MODE_SILENT
from . import COMMAND_PREFIX, Context, SlackParser

NAME = "silent"
ALIASES = ("quiet", "mentions")
SUMMARY = "only answer in this thread when tagged"

_DESCRIPTION = f"""\
Make the agent mention-only in the thread you type this in.

What still gets through: a real @-tag of the bot, or a direct message. Nothing else
— an ordinary reply in the thread is dropped by the daemon before Claude ever sees
it. Typing the bot's name as plain text is NOT a tag; use the autocomplete so it
becomes a real mention.

Why this exists next to the normal behaviour: by default the agent is shown every
reply in a thread it is part of and decides for itself whether the message was for
it. That judgement costs a turn per message and can be wrong. This mode costs
nothing at all, because the decision is made out here on rules rather than in there
on judgement.

Nothing is lost while it is quiet: the next time you tag it, the messages it was not
shown come with the mention, so you do not have to repeat the conversation.

Compared with the other two:
  {COMMAND_PREFIX}silent   answers when tagged            <- this
  {COMMAND_PREFIX}pause    answers nothing at all
  {COMMAND_PREFIX}resume   back to judging for itself

Per thread, and it survives a restart. Other threads are unaffected.
"""


def build_parser() -> argparse.ArgumentParser:
    return SlackParser(
        prog=f"{COMMAND_PREFIX}{NAME}",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


async def run(ctx: Context, args: argparse.Namespace) -> None:
    previous = ctx.store.set_thread_mode(
        ctx.channel, ctx.thread_ts, MODE_SILENT, ctx.user
    )

    if previous == MODE_SILENT:
        await ctx.say(
            "Already mention-only here. Tag me and I answer; anything else in this "
            f"thread I never see. `{COMMAND_PREFIX}resume` to go back to normal."
        )
        return

    if previous == MODE_PAUSED:
        # This LOOSENS the mute, and |silent sounds stricter than |pause, so saying
        # so is the difference between a clear command and a nasty surprise.
        await ctx.say(
            ":large_orange_circle: :arrow_right: :speech_balloon: *Lifted the pause* "
            "— this thread was fully muted, and now I answer when tagged. That is "
            f"less quiet than it was. `{COMMAND_PREFIX}pause` to mute it again, "
            f"`{COMMAND_PREFIX}resume` for normal."
        )
        return

    unknown = ctx.store.find_session(ctx.channel, ctx.thread_ts) is None
    note = (
        "\nI have no session in this thread yet, so this takes effect the moment "
        "there is one."
        if unknown else ""
    )
    await ctx.say(
        ":speech_balloon: Mention-only in this thread now. Tag me and I answer; "
        "everything else said here is dropped before it reaches Claude, so it costs "
        "nothing. When you next tag me I am shown what I missed. "
        f"`{COMMAND_PREFIX}resume` to go back to normal.{note}"
    )
