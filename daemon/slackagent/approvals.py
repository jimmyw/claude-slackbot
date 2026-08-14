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
from .grants import MATCH_ANY, MATCH_EXACT, suggest
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
    requested_by: str = ""
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
        # A Slack button value is limited and a compound command can suggest
        # several patterns, so the suggestion stays here rather than riding in the
        # payload the browser hands back.
        self._suggestions: dict[str, Any] = {}
        self._runner: web.AppRunner | None = None

    # -- run registration ---------------------------------------------------

    def register_run(
        self, run_token: str, channel_id: str, thread_ts: str, session_id: str,
        requested_by: str = "",
    ) -> None:
        self._runs[run_token] = PendingRun(
            channel_id, thread_ts, session_id, requested_by
        )

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
        if granted:
            # A list: a compound command needs one grant per segment, and naming
            # them all is what makes an auto-approval auditable after the fact.
            names = ", ".join(f"#{x.id} {x.pattern}" for x in granted)
            log.info("auto-approved %s by grant(s) %s", tool_name, names)
            return web.json_response({
                "approved": True,
                "reason": f"covered by grant(s) {names}",
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
            run.requested_by,
        )

        future: asyncio.Future[Verdict] = asyncio.get_running_loop().create_future()
        self._waiters[approval_id] = future
        run.approval_ids.add(approval_id)

        try:
            hint = suggest(tool_name, tool_input)
            if hint is not None:
                self._suggestions[approval_id] = hint
            posted = await self._slack.chat_postMessage(
                channel=run.channel_id,
                thread_ts=run.thread_ts,
                text=f"Approval needed: {tool_name}",
                blocks=_approval_blocks(
                    approval_id, tool_name, tool_input, hint,
                    requester=(
                        run.requested_by
                        if run.requested_by
                        and run.requested_by != self._config.authorized_user
                        else None
                    ),
                ),
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
            self._suggestions.pop(approval_id, None)
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
        approval_id = action.get("value") or ""
        action_id = action.get("action_id")
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
                        f":no_entry: Only <@{self._config.authorized_user}> can "
                        "approve or deny tool calls. Your request is still "
                        "pending — they will see it in this thread."
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

        created: list[str] = []
        hint = self._suggestions.get(approval_id)
        if action_id == ACTION_ALWAYS and hint is not None:
            # One press may create several grants: `cd x && npm test` needs both
            # `cd` and `npm test` before that command is covered.
            for pattern in hint.patterns:
                grant_id = await asyncio.to_thread(
                    self._store.add_grant,
                    row["tool_name"], pattern, clicker, hint.match_type,
                )
                created.append(f"#{grant_id} {pattern}")
            log.info(
                "grants created by %s for %s (%s): %s",
                clicker, row["tool_name"], hint.match_type, ", ".join(created),
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


def _granted_suffix(tool_name: str, hint: Any) -> str:
    if hint.match_type == MATCH_ANY:
        return f" — always allowing all `{tool_name}`"
    if hint.match_type == MATCH_EXACT:
        return " — always allowing that exact command"
    return " — always allowing " + ", ".join(f"`{p}`" for p in hint.patterns)


def _approval_blocks(
    approval_id: str, tool_name: str, tool_input: Any, hint: Any = None,
    requester: str | None = None,
) -> list[dict]:
    # Naming the requester is the point of letting guests talk: the approver has to
    # be able to see that this action is not their own.
    heading = f":lock: *Approval needed* — `{tool_name}`"
    if requester:
        heading += f"\nrequested by <@{requester}>"
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": heading},
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
                                "text": _always_label(tool_name, hint)[:75],
                            },
                            "action_id": ACTION_ALWAYS,
                            "value": approval_id,
                        }
                    ]
                    if hint is not None
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


def _always_label(tool_name: str, hint: Any) -> str:
    if hint.match_type == MATCH_ANY:
        return f"Always allow all {tool_name}"
    if hint.match_type == MATCH_EXACT:
        return "Always allow this exact command"
    return "Always allow: " + ", ".join(hint.patterns)


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
