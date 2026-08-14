"""stream-json -> Slack.

Posts a placeholder as soon as a run starts, then edits it as events arrive.
Edits are throttled because Slack allows roughly one message write per second per
channel, and a chatty run emits events far faster than that.

Event types seen in practice from `claude -p --output-format stream-json
--verbose` (2.1.231): system/init, rate_limit_event, assistant, user, result.
Unknown types are ignored rather than treated as errors — the stream is free to
grow new ones.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .mrkdwn import to_mrkdwn

log = logging.getLogger(__name__)

# Slack rejects a section block whose text exceeds 3000 characters.
BLOCK_LIMIT = 2900


class SlackRenderer:
    def __init__(
        self,
        slack_client: Any,
        channel: str,
        thread_ts: str,
        *,
        update_interval_s: float = 1.2,
    ) -> None:
        self._slack = slack_client
        self._channel = channel
        self._thread_ts = thread_ts
        self._interval = update_interval_s

        self._message_ts: str | None = None
        self._text_parts: list[str] = []
        self._activity: list[str] = []
        self._footer: str | None = None
        self._last_update = 0.0
        self._dirty = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        posted = await self._slack.chat_postMessage(
            channel=self._channel,
            thread_ts=self._thread_ts,
            text="_working…_",
        )
        self._message_ts = posted["ts"]

    async def handle(self, event: dict) -> None:
        """Fold one stream event into the rendered message."""
        kind = event.get("type")

        if kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                block_type = block.get("type")
                if block_type == "text":
                    self._text_parts.append(block.get("text") or "")
                    self._dirty = True
                elif block_type == "tool_use":
                    self._activity.append(
                        f"🔧 {_describe_tool(block.get('name'), block.get('input'))}"
                    )
                    self._dirty = True

        elif kind == "result":
            self._footer = _result_footer(event)
            if event.get("is_error"):
                text = event.get("result") or "the run reported an error"
                self._text_parts.append(f"\n:warning: {text}")
            self._dirty = True
            await self.flush(force=True)
            return

        elif kind == "_bridge":
            if event.get("exit_code"):
                stderr = (event.get("stderr") or "").strip()
                tail = stderr.splitlines()[-3:] if stderr else []
                detail = ("\n```\n" + "\n".join(tail) + "\n```") if tail else ""
                self._text_parts.append(
                    f"\n:warning: The run ended unexpectedly "
                    f"(exit {event['exit_code']}).{detail}"
                )
                self._dirty = True
            await self.flush(force=True)
            return

        elif kind == "rate_limit_event":
            # Informational; nothing useful to show mid-run.
            return

        await self.flush()

    async def flush(self, *, force: bool = False) -> None:
        async with self._lock:
            if not self._dirty or self._message_ts is None:
                return
            now = time.monotonic()
            if not force and now - self._last_update < self._interval:
                return

            try:
                await self._slack.chat_update(
                    channel=self._channel,
                    ts=self._message_ts,
                    text=self._plain_text(),
                    blocks=self._blocks(),
                )
                self._last_update = now
                self._dirty = False
            except Exception:
                log.exception("could not update the Slack message")

    async def fail(self, message: str) -> None:
        self._text_parts.append(f"\n:warning: {message}")
        self._dirty = True
        await self.flush(force=True)

    # -- rendering ----------------------------------------------------------

    def _plain_text(self) -> str:
        # The fallback field is what notifications and unfurl previews show, so it
        # is converted too rather than left as raw Markdown.
        text = to_mrkdwn("".join(self._text_parts).strip()) or "_working…_"
        return text[:2900]

    def _blocks(self) -> list[dict]:
        blocks: list[dict] = []
        # Claude writes GitHub Markdown; Slack renders mrkdwn. Without this the
        # message arrives showing literal ##, ** and [text](url).
        body = to_mrkdwn("".join(self._text_parts).strip())

        if self._activity:
            recent = self._activity[-6:]
            hidden = len(self._activity) - len(recent)
            lines = list(recent)
            if hidden > 0:
                lines.insert(0, f"_…{hidden} earlier steps_")
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": "\n".join(lines)[:BLOCK_LIMIT]}
                    ],
                }
            )

        if body:
            for chunk in _chunk(body, BLOCK_LIMIT):
                blocks.append(
                    {"type": "section", "text": {"type": "mrkdwn", "text": chunk}}
                )
        elif not self._activity:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "_working…_"},
                }
            )

        if self._footer:
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": self._footer}],
                }
            )

        # Slack caps a message at 50 blocks. Long runs get the head and tail;
        # the full text is always in the fallback `text` field.
        if len(blocks) > 50:
            blocks = blocks[:47] + [
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": "_output truncated_"}],
                }
            ] + blocks[-2:]
        return blocks


def _chunk(text: str, size: int) -> list[str]:
    """Split on paragraph boundaries where possible, hard-split when not.

    Fence-aware: a split inside a ``` block would leave one chunk unterminated and
    the next starting with a stray fence, so Slack would render the first as an
    endless code block and the second as prose containing ```. Any chunk with an odd
    number of fences is closed, and the following one reopened.
    """
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > size:
        window = remaining[:size]
        split = window.rfind("\n\n")
        if split < size // 2:
            split = window.rfind("\n")
        if split < size // 2:
            split = size
        chunks.append(remaining[:split])
        remaining = remaining[split:].lstrip("\n")
    if remaining:
        chunks.append(remaining)

    balanced: list[str] = []
    carry_open = False
    for chunk in chunks:
        if carry_open:
            chunk = "```\n" + chunk
        # Reserve room for the fences added on either side.
        if chunk.count("```") % 2:
            chunk = chunk + "\n```"
            carry_open = True
        else:
            carry_open = False
        balanced.append(chunk)
    return balanced


def _describe_tool(name: str | None, tool_input: Any) -> str:
    name = name or "tool"
    if not isinstance(tool_input, dict):
        return name

    for key in ("file_path", "path", "pattern", "command", "url", "query"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            summary = value if len(value) <= 90 else value[:90] + "…"
            return f"{name}({summary})"
    return name


def _result_footer(event: dict) -> str:
    bits: list[str] = []

    duration_ms = event.get("duration_ms")
    if isinstance(duration_ms, (int, float)):
        bits.append(f"{duration_ms / 1000:.1f}s")

    cost = event.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        bits.append(f"${cost:.4f}")

    turns = event.get("num_turns")
    if isinstance(turns, int):
        bits.append(f"{turns} turn{'s' if turns != 1 else ''}")

    denials = event.get("permission_denials") or []
    if denials:
        names = sorted({d.get("tool_name", "?") for d in denials})
        bits.append(f"{len(denials)} denied ({', '.join(names)})")

    return " · ".join(bits) if bits else ""
