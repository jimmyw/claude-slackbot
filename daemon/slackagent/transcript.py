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
  * **No watermark is the whole thread, but only when asked.** A thread the bot has
    never spoken in has no watermark, and the first mention in it is exactly the
    case where the history is the question ("can you fix that?"). It is fetched when
    `cold_start` is set — `CATCH_UP_NEW_THREADS`, on by default — and the same
    bounds in `prompt.transcript_block` apply, so a busy thread costs the same as a
    long gap does. With it off, no watermark means "start here" and nothing is read.

The fetch walks to the END of the thread, not the first page of it.
`conversations.replies` returns a window oldest-first, so a single capped call
yields the OLDEST messages in it — the wrong end, since the mention is almost
always about the newest. Reaching the end can take several pages; when it cannot be
reached at all, nothing is quoted, because quoting the start of a 2000-message
thread as "what you missed" points the agent away from the question.
"""
from __future__ import annotations

import asyncio
import logging
import secrets

from . import commands
from .prompt import MAX_MESSAGES, transcript_block

log = logging.getLogger(__name__)

FETCH_TIMEOUT_S = 10.0

# One page, and how many of them. 200 is Slack's ceiling for conversations.replies;
# ten pages is 2000 messages, past which a thread is not a conversation anyone is
# asking about the tail of.
PAGE_LIMIT = 200
MAX_PAGES = 10

# Subtypes worth quoting. Everything else — joins, leaves, tombstones, edits — is
# already refused on the live path in _on_message, and a transcript must not smuggle
# in what the live path declines to forward.
_QUOTABLE_SUBTYPES = {None, "thread_broadcast"}


class _TailUnreachable(RuntimeError):
    """The newest end of the window could not be reached within the page budget."""


async def catch_up(
    client,  # noqa: ANN001  slack_sdk AsyncWebClient
    *,
    channel: str,
    thread_ts: str,
    since_ts: str | None,
    current_ts: str,
    bot_user_id: str | None,
    bot_id: str | None,
    cold_start: bool = False,
) -> str | None:
    """Quoted context for a mention that follows a gap, or None.

    `since_ts` is the watermark; None means nothing from this thread has ever been
    forwarded. `cold_start` decides what that means: the thread so far, or nothing.

    Never raises. A failure here must cost the reply nothing: the turn goes ahead
    without context, and the log line is the trace.
    """
    if bot_user_id is None:
        # Without our own id we could not reliably exclude our own messages, and
        # feeding the agent its own prose back is the worst of the available failures.
        return None
    if since_ts is None and not cold_start:
        return None
    if current_ts == thread_ts:
        # The mention IS the thread root, so there is nothing before it.
        return None

    try:
        entries, incomplete = await asyncio.wait_for(
            _collect(
                client,
                channel=channel,
                thread_ts=thread_ts,
                since_ts=since_ts,
                current_ts=current_ts,
                bot_user_id=bot_user_id,
                bot_id=bot_id,
            ),
            timeout=FETCH_TIMEOUT_S,
        )
    except _TailUnreachable:
        log.warning(
            "gave up reading %s/%s: more than %d pages before the newest message, "
            "so the tail is out of reach and quoting the start would mislead",
            channel, thread_ts, MAX_PAGES,
        )
        return None
    except Exception:
        log.warning(
            "could not fetch missed messages for %s/%s; continuing without them",
            channel, thread_ts, exc_info=True,
        )
        return None

    if not entries:
        return None

    log.info(
        "quoting %d missed message(s) for %s/%s%s",
        len(entries), channel, thread_ts, " (from the thread's start)" if not since_ts else "",
    )
    # A fresh nonce per fetch: the fence has to be unforgeable by anyone writing in
    # the thread, and a fixed tag could simply be typed.
    return transcript_block(entries, nonce=secrets.token_hex(3), incomplete=incomplete)


async def _collect(
    client,  # noqa: ANN001
    *,
    channel: str,
    thread_ts: str,
    since_ts: str | None,
    current_ts: str,
    bot_user_id: str,
    bot_id: str | None,
) -> tuple[list[tuple[str, str]], bool]:
    """Read the window up to `current_ts`, keeping its newest quotable messages.

    Returns (entries oldest-first, whether older ones were dropped to get there).
    Raises `_TailUnreachable` if the page budget ran out with more to come: what is
    held at that point is the START of the window, which is not what was asked for.
    """
    # A little more than will be quoted, so `transcript_block` still chooses on a
    # full budget rather than on whatever a page boundary happened to leave.
    keep = MAX_MESSAGES * 3
    entries: list[tuple[str, str]] = []
    dropped_older = False
    cursor = ""

    for _ in range(MAX_PAGES):
        params = {
            "channel": channel,
            "ts": thread_ts,
            "latest": current_ts,
            "inclusive": False,
            "limit": PAGE_LIMIT,
        }
        if since_ts:
            # Only as a payload bound. The filtering below is what actually decides:
            # conversations.replies returns the thread parent whatever `oldest` says.
            params["oldest"] = since_ts
        if cursor:
            params["cursor"] = cursor

        response = await client.conversations_replies(**params)
        entries.extend(
            _quotable(
                response.get("messages") or [],
                since_ts=since_ts,
                current_ts=current_ts,
                bot_user_id=bot_user_id,
                bot_id=bot_id,
            )
        )
        if len(entries) > keep:
            # The newest are the ones the mention is about; say so rather than
            # silently presenting a shortened conversation as a whole one.
            del entries[: len(entries) - keep]
            dropped_older = True

        if not response.get("has_more"):
            return entries, dropped_older
        cursor = (response.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            # has_more with nowhere to go. Same outcome as running out of pages:
            # we are holding the wrong end of the window.
            break

    raise _TailUnreachable


def _quotable(
    messages: list[dict],
    *,
    since_ts: str | None,
    current_ts: str,
    bot_user_id: str,
    bot_id: str | None,
) -> list[tuple[str, str]]:
    """Filter a conversations.replies payload down to what may be quoted.

    The API bounds are not trusted to do this: `conversations.replies` always returns
    the thread parent whatever `oldest` says, and the current message's visibility is
    subject to eventual consistency. The bounds keep the payload small; this decides.

    With no watermark the floor is 0, so the thread parent IS quotable — on a cold
    start it is usually the message the whole thread is about.
    """
    floor = _as_float(since_ts) if since_ts else 0.0
    # `or inf`: a ts we cannot parse must not filter out the whole page.
    ceiling = _as_float(current_ts) or float("inf")
    entries: list[tuple[str, str]] = []

    for message in messages:
        ts = message.get("ts") or ""
        if ts == current_ts or _as_float(ts) <= floor:
            continue
        if _as_float(ts) >= ceiling:
            # A page fetched with a cursor can reach past `latest`; the mention
            # itself and anything after it is not its own context.
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


def _as_float(ts: str | None) -> float:
    """Slack timestamps compare numerically, not lexicographically."""
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0
