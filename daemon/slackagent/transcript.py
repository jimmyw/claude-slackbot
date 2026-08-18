"""What the agent missed: the messages in a thread it was never shown.

Gap-driven, not mode-driven. `threads.last_seen_ts` is the newest message forwarded
to Claude from a thread; when the bot is tagged and newer messages exist, they are
fetched and quoted so "can you look at that?" is answerable. That covers a
`|silent` stretch, and equally a stretch when the daemon was simply down.

Two invariants, both learned the hard way in review:

  * **A pause is never backfilled.** `|pause` promises that nothing said in the
    thread reaches Claude, so `|resume` moves the watermark forward and this module
    never sees that window. The asymmetry is deliberate: `|silent` DOES backfill,
    because being told what you missed is the point of it.
  * **No watermark means "start here".** A thread that has never forwarded anything
    has no gap — not a gap reaching back to the beginning of the thread.
"""
from __future__ import annotations

import asyncio
import logging
import secrets

from . import commands
from .prompt import MAX_MESSAGES, transcript_block

log = logging.getLogger(__name__)

FETCH_TIMEOUT_S = 10.0

# Subtypes worth quoting. Everything else — joins, leaves, tombstones, edits — is
# already refused on the live path in _on_message, and a transcript must not smuggle
# in what the live path declines to forward.
_QUOTABLE_SUBTYPES = {None, "thread_broadcast"}


async def catch_up(
    client,  # noqa: ANN001  slack_sdk AsyncWebClient
    *,
    channel: str,
    thread_ts: str,
    since_ts: str | None,
    current_ts: str,
    bot_user_id: str | None,
    bot_id: str | None,
) -> str | None:
    """Quoted context for a mention that follows a gap, or None.

    Never raises. A failure here must cost the reply nothing: the turn goes ahead
    without context, and the log line is the trace.
    """
    if not since_ts or bot_user_id is None:
        # No watermark: nothing to catch up on. No self-id: we could not reliably
        # exclude our own messages, and feeding the agent its own prose back is the
        # worst of the available failures.
        return None
    if current_ts == thread_ts:
        # The mention IS the thread root, so there is nothing before it.
        return None

    try:
        response = await asyncio.wait_for(
            client.conversations_replies(
                channel=channel,
                ts=thread_ts,
                oldest=since_ts,
                latest=current_ts,
                inclusive=False,
                # Ask for more than we will keep: the parent message always comes
                # back regardless of `oldest`, and filtering happens below.
                limit=min(MAX_MESSAGES * 3, 200),
            ),
            timeout=FETCH_TIMEOUT_S,
        )
        messages = response.get("messages") or []
        incomplete = bool(response.get("has_more"))
    except Exception:
        log.warning(
            "could not fetch missed messages for %s/%s; continuing without them",
            channel, thread_ts, exc_info=True,
        )
        return None

    entries = _quotable(
        messages,
        since_ts=since_ts,
        current_ts=current_ts,
        bot_user_id=bot_user_id,
        bot_id=bot_id,
    )
    if not entries:
        return None

    log.info(
        "quoting %d missed message(s) for %s/%s", len(entries), channel, thread_ts
    )
    # A fresh nonce per fetch: the fence has to be unforgeable by anyone writing in
    # the thread, and a fixed tag could simply be typed.
    return transcript_block(entries, nonce=secrets.token_hex(3), incomplete=incomplete)


def _quotable(
    messages: list[dict],
    *,
    since_ts: str,
    current_ts: str,
    bot_user_id: str,
    bot_id: str | None,
) -> list[tuple[str, str]]:
    """Filter a conversations.replies payload down to what may be quoted.

    The API bounds are not trusted to do this: `conversations.replies` always returns
    the thread parent whatever `oldest` says, and the current message's visibility is
    subject to eventual consistency. The bounds keep the payload small; this decides.
    """
    floor = _as_float(since_ts)
    entries: list[tuple[str, str]] = []

    for message in messages:
        ts = message.get("ts") or ""
        if ts == current_ts or _as_float(ts) <= floor:
            continue
        if message.get("subtype") not in _QUOTABLE_SUBTYPES:
            continue
        # Ours: by bot_id when we have it, by user id always. A bot message carries
        # BOTH a bot_id and a user, so testing user alone is not enough.
        if message.get("bot_id") and message.get("bot_id") == bot_id:
            continue
        if message.get("bot_id") and bot_id is None:
            continue
        if message.get("user") == bot_user_id:
            continue
        text = (message.get("text") or "").strip()
        if not text or commands.is_local_command(text):
            # An operator command was deliberately never shown to the agent; quoting
            # it back would undo that.
            continue
        entries.append((message.get("user") or "", text))

    # Slack returns a thread oldest-first, which is the order they are quoted in.
    return entries


def _as_float(ts: str) -> float:
    """Slack timestamps compare numerically, not lexicographically."""
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0
