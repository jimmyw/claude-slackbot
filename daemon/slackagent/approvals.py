"""The approval gate.

An aiohttp listener on loopback receives pending tool calls from the VM's hook
(over the SSH reverse tunnel), posts an Approve/Deny message into the originating
Slack thread, and holds the HTTP request open until someone clicks or the window
closes.

Two rules this module exists to enforce:

  1. Only AUTHORIZED_USER_ID can decide. Channel membership is not an access
     control; anyone in the channel can see the buttons and click them. The
     user-ID check on the button payload is the only thing that actually gates.

  2. Timeout means deny. If nobody answers, the tool call does not happen.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

from .config import Config
from .grants import suggest_pattern
from .store import Store

log = logging.getLogger(__name__)

ACTION_APPROVE = "agent_approve"
ACTION_DENY = "agent_deny"
ACTION_ALWAYS = "agent_always"


@dataclass
class PendingRun:
    """Where to post approvals for one in-flight run."""

    channel_id: str
    thread_ts: str
    session_id: str
    approval_ids: set[str] = field(default_factory=set)


@dataclass
class Verdict:
    approved: bool
    reason: str


class ApprovalService:
    def __init__(self, config: Config, store: Store, slack_client: Any) -> None:
        self._config = config
        self._store = store
        self._slack = slack_client
        self._runs: dict[str, PendingRun] = {}
        self._waiters: dict[str, asyncio.Future[Verdict]] = {}
        self._runner: web.AppRunner | None = None

    # -- run registration ---------------------------------------------------

    def register_run(
        self, run_token: str, channel_id: str, thread_ts: str, session_id: str
    ) -> None:
        self._runs[run_token] = PendingRun(channel_id, thread_ts, session_id)

    def unregister_run(self, run_token: str) -> None:
        """Drop a finished run and fail any approval still waiting on it."""
        run = self._runs.pop(run_token, None)
        if run is None:
            return
        # Iterate a copy: a concurrent _handle_approve finally-block discards from
        # this set, which would otherwise mutate it mid-iteration.
        for approval_id in list(run.approval_ids):
            future = self._waiters.pop(approval_id, None)
            if future is not None and not future.done():
                future.set_result(
                    Verdict(False, "the run ended before this was approved")
                )

    # -- HTTP listener ------------------------------------------------------

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/approve", self._handle_approve)
        app.router.add_get("/health", lambda _: web.json_response({"ok": True}))

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner, self._config.approval_host, self._config.approval_port
        )
        await site.start()
        log.info(
            "approval listener on %s:%s",
            self._config.approval_host,
            self._config.approval_port,
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle_approve(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"error": "bad json"}, status=400)

        run_token = body.get("run_token")
        run = self._runs.get(run_token) if run_token else None
        if run is None:
            # Unknown token: either a stale hook from a finished run, or someone
            # poking the loopback port. Deny without posting anything to Slack.
            log.warning("approval request for unknown run_token")
            return web.json_response({"error": "unknown run"}, status=403)

        tool_name = body.get("tool_name") or "<unknown>"
        tool_input = body.get("tool_input")

        # An existing grant answers without a button. Checked here, on the host:
        # the guest only ever asks, so it cannot grant itself anything.
        granted = await asyncio.to_thread(self._store.find_grant, tool_name, tool_input)
        if granted is not None:
            log.info(
                "auto-approved %s by grant #%s (%r)",
                tool_name, granted.id, granted.pattern,
            )
            return web.json_response({
                "approved": True,
                "reason": f"covered by grant #{granted.id}: {granted.pattern}",
            })

        approval_id = uuid.uuid4().hex

        await asyncio.to_thread(
            self._store.open_approval,
            approval_id,
            run.channel_id,
            run.thread_ts,
            run.session_id,
            tool_name,
            json.dumps(tool_input, ensure_ascii=False),
            body.get("tool_use_id"),
        )

        future: asyncio.Future[Verdict] = asyncio.get_running_loop().create_future()
        self._waiters[approval_id] = future
        run.approval_ids.add(approval_id)

        try:
            pattern = suggest_pattern(tool_name, tool_input)
            posted = await self._slack.chat_postMessage(
                channel=run.channel_id,
                thread_ts=run.thread_ts,
                text=f"Approval needed: {tool_name}",
                blocks=_approval_blocks(approval_id, tool_name, tool_input, pattern),
            )
            message_ts = posted["ts"]
            await asyncio.to_thread(
                self._store.attach_message_ts, approval_id, message_ts
            )
        except Exception:
            log.exception("could not post the approval request")
            self._waiters.pop(approval_id, None)
            run.approval_ids.discard(approval_id)
            await asyncio.to_thread(
                self._store.resolve_approval, approval_id, "denied", None
            )
            return web.json_response(
                {"approved": False, "reason": "could not reach Slack to ask"}
            )

        try:
            verdict = await asyncio.wait_for(
                future, timeout=self._config.approval_timeout_s
            )
        except asyncio.TimeoutError:
            verdict = Verdict(
                False,
                f"no answer within {self._config.approval_timeout_s}s — denied",
            )
            await asyncio.to_thread(
                self._store.resolve_approval, approval_id, "timeout", None
            )
            await self._finalize_message(
                run.channel_id, message_ts, tool_name, "Timed out — denied"
            )
        finally:
            self._waiters.pop(approval_id, None)
            run.approval_ids.discard(approval_id)

        return web.json_response(
            {"approved": verdict.approved, "reason": verdict.reason}
        )

    # -- button handling ----------------------------------------------------

    async def handle_button(self, body: dict, action: dict, respond: Any) -> None:
        """Resolve an approval from a Slack button click.

        Wired to both action IDs in app.py. `respond` sends an ephemeral message
        visible only to the clicker.
        """
        clicker = (body.get("user") or {}).get("id")
        raw = action.get("value") or ""
        action_id = action.get("action_id")
        # The "always" button carries the pattern after the approval id, since a
        # Slack button value is the only state it can hand back.
        approval_id, _, pattern = raw.partition("|")
        approve = action_id in (ACTION_APPROVE, ACTION_ALWAYS)

        # THE access control. Everything else in this file is bookkeeping.
        if clicker != self._config.authorized_user:
            log.warning(
                "rejected approval click from unauthorized user %s", clicker
            )
            await respond(
                {
                    "response_type": "ephemeral",
                    "text": (
                        ":no_entry: Only the authorized operator can approve or "
                        "deny tool calls. This request is still pending."
                    ),
                }
            )
            return

        row = await asyncio.to_thread(self._store.approval, approval_id)
        if row is None:
            await respond(
                {"response_type": "ephemeral", "text": "That approval is unknown."}
            )
            return

        claimed = await asyncio.to_thread(
            self._store.resolve_approval,
            approval_id,
            "approved" if approve else "denied",
            clicker,
        )
        if not claimed:
            await respond(
                {
                    "response_type": "ephemeral",
                    "text": f"Already resolved ({row['state']}).",
                }
            )
            return

        if action_id == ACTION_ALWAYS and pattern:
            grant_id = await asyncio.to_thread(
                self._store.add_grant, row["tool_name"], pattern, clicker
            )
            log.info(
                "grant #%s created by %s: %s %r",
                grant_id, clicker, row["tool_name"], pattern,
            )

        future = self._waiters.get(approval_id)
        if future is not None and not future.done():
            future.set_result(
                Verdict(
                    approve,
                    "approved in Slack" if approve else "denied in Slack",
                )
            )

        await self._finalize_message(
            row["channel_id"],
            row["message_ts"],
            row["tool_name"],
            f"{'Approved' if approve else 'Denied'} by <@{clicker}>",
        )

    async def _finalize_message(
        self, channel: str, message_ts: str | None, tool_name: str, outcome: str
    ) -> None:
        """Replace the buttons with the outcome, so nothing stays clickable."""
        if not message_ts:
            return
        try:
            await self._slack.chat_update(
                channel=channel,
                ts=message_ts,
                text=f"{tool_name}: {outcome}",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{tool_name}* — {outcome}",
                        },
                    }
                ],
            )
        except Exception:
            log.exception("could not update the approval message")


def _approval_blocks(
    approval_id: str, tool_name: str, tool_input: Any, pattern: str | None = None
) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":lock: *Approval needed* — `{tool_name}`",
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": _format_input(tool_input)},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "action_id": ACTION_APPROVE,
                    "value": approval_id,
                },
                *(
                    [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                # Truncated: Slack rejects button text over 75 chars.
                                "text": f"Always allow: {pattern}"[:75],
                            },
                            "action_id": ACTION_ALWAYS,
                            "value": f"{approval_id}|{pattern}",
                        }
                    ]
                    if pattern
                    else []
                ),
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "action_id": ACTION_DENY,
                    "value": approval_id,
                },
            ],
        },
    ]


def _format_input(tool_input: Any, limit: int = 2500) -> str:
    """Render the tool input for review, inside Slack's block text budget."""
    if isinstance(tool_input, dict):
        parts = []
        for key, value in tool_input.items():
            rendered = value if isinstance(value, str) else json.dumps(value)
            if len(rendered) > 900:
                rendered = rendered[:900] + f"\n… (+{len(rendered) - 900} chars)"
            parts.append(f"{key}: {rendered}")
        text = "\n".join(parts)
    else:
        text = json.dumps(tool_input, indent=2, ensure_ascii=False)

    if len(text) > limit:
        text = text[:limit] + "\n… (truncated)"
    return f"```\n{text}\n```"
