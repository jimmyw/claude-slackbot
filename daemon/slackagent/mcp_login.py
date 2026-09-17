"""`|mcp login` — refresh a header-based MCP credential from Slack.

Some upstreams (syslog) authenticate with a static header rather than OAuth, and
OAuth (mcpproxy.OAuth) already refreshes itself on the host — there is nothing to
log in for there. A header credential has no such self-repair: when it goes
stale, the only fix used to be SSH into terra and hand-edit the config file.

This follows the same split OAuth already uses: the config file only bootstraps a
credential, and once one is set here it is authoritative
(`Store.mcp_header_override`, read in `McpProxy._default_upstream`). The path in
is a Slack modal, not a typed command, because a plain `|mcp login syslog
<value>` would sit in the thread's message history forever and could land in the
daemon log the moment anything downstream raised — a modal's submission never
becomes a channel message or a log line.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from . import mcpconfig
from .config import Config
from .store import Store

log = logging.getLogger(__name__)

ACTION_LOGIN_START = "mcp_login_start"
VIEW_LOGIN_MODAL = "mcp_login_modal"

_BLOCK_ID = "cred"
_ACTION_ID = "value"


class McpLoginService:
    """Posts the button, opens the modal, and is the only place the pasted value
    is ever handled — it goes straight from the submission payload into sqlite
    and is never logged or echoed back to Slack."""

    def __init__(
        self, config: Config, store: Store, registry: Any, slack_client: Any
    ) -> None:
        self._config = config
        self._store = store
        self._registry = registry
        self._slack = slack_client

    def _ineligible_reason(self, server: Any) -> str:
        """Empty string when a login makes sense for this server, else why not."""
        if server.type != mcpconfig.HTTP:
            return (
                f"`{server.name}` is a {server.type} server — there's no HTTP "
                "header to log in with."
            )
        if server.shared.oauth or any(c.oauth for c in server.per_user.values()):
            return (
                f"`{server.name}` uses OAuth, which already refreshes itself on "
                "the host. There's nothing to log in for."
            )
        return ""

    # -- |mcp login / |mcp logout --------------------------------------------

    async def request(
        self, channel: str, thread_ts: str, server_name: str, user: str
    ) -> str:
        """Post the "enter credential" button. Returns an error string instead
        if this server isn't a fit for a header login, so the command can report
        it as a normal CommandError."""
        server = self._registry.get(server_name)
        if server is None:
            return f"no MCP server called `{server_name}`."
        reason = self._ineligible_reason(server)
        if reason:
            return reason

        payload = json.dumps(
            {"server": server.name, "channel": channel, "thread_ts": thread_ts}
        )
        await self._slack.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"Update the credential for `{server.name}`?",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"<@{user}>, click below to paste a fresh credential "
                            f"for `{server.name}`. It goes straight into a Slack "
                            "modal — never into this thread, never into a log."
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Enter credential"},
                            "action_id": ACTION_LOGIN_START,
                            "value": payload,
                        }
                    ],
                },
            ],
        )
        return ""

    # -- button → modal -------------------------------------------------------

    async def handle_button(self, body: dict, action: dict) -> None:
        clicker = (body.get("user") or {}).get("id") or ""
        if clicker != self._config.authorized_user:
            # Same posture as ApprovalService: anyone in the channel can see and
            # click the button, but only the operator may act on it. Writing a
            # credential is more sensitive than approving one tool call, so this
            # checks the operator specifically rather than the wider approver set.
            log.warning("rejected mcp login click from %s", clicker)
            return

        try:
            clicked = json.loads(action.get("value") or "{}")
        except json.JSONDecodeError:
            log.warning("mcp login: unparseable button payload")
            return

        server_name = clicked.get("server") or ""
        if self._registry.get(server_name) is None:
            return
        trigger_id = body.get("trigger_id")
        if not trigger_id:
            return

        private_metadata = json.dumps(
            {
                "server": server_name,
                "channel": clicked.get("channel", ""),
                "thread_ts": clicked.get("thread_ts", ""),
                "user": clicker,
            }
        )
        await self._slack.views_open(
            trigger_id=trigger_id,
            view={
                "type": "modal",
                "callback_id": VIEW_LOGIN_MODAL,
                "private_metadata": private_metadata,
                "title": {"type": "plain_text", "text": f"{server_name} login"[:24]},
                "submit": {"type": "plain_text", "text": "Save"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": [
                    {
                        "type": "input",
                        "block_id": _BLOCK_ID,
                        "label": {"type": "plain_text", "text": "New Cookie header"},
                        "hint": {
                            "type": "plain_text",
                            "text": (
                                "The whole name=value pair, exactly as sent in a "
                                "browser's Cookie request header — not just the "
                                "value from a devtools \"Value\" column, which is "
                                "missing the name and will be rejected."
                            ),
                        },
                        "element": {
                            "type": "plain_text_input",
                            "action_id": _ACTION_ID,
                            "multiline": True,
                            "placeholder": {
                                "type": "plain_text",
                                "text": "name=value",
                            },
                        },
                    }
                ],
            },
        )

    # -- modal submission -------------------------------------------------------

    async def handle_submit(self, body: dict) -> dict:
        view = body.get("view") or {}
        try:
            meta = json.loads(view.get("private_metadata") or "{}")
        except json.JSONDecodeError:
            meta = {}

        server_name = meta.get("server") or ""
        channel = meta.get("channel") or ""
        thread_ts = meta.get("thread_ts") or ""
        expected_user = meta.get("user") or ""

        submitter = (body.get("user") or {}).get("id") or ""
        # Re-checked here too: the button check keeps a guest from opening the
        # modal at all, but a belt-and-braces check on the write itself costs
        # nothing and means this is never the only thing standing between a
        # credential and someone who is not the operator.
        if submitter != self._config.authorized_user or submitter != expected_user:
            log.warning("rejected mcp login submission from %s", submitter)
            return {}

        server = self._registry.get(server_name)
        if server is None:
            return {}

        values = (view.get("state") or {}).get("values") or {}
        raw = (
            values.get(_BLOCK_ID, {}).get(_ACTION_ID, {}).get("value") or ""
        )
        value = raw.strip()
        if not value:
            return {
                "response_action": "errors",
                "errors": {_BLOCK_ID: "Paste a value first."},
            }
        if "=" not in value:
            # The one mistake this modal actually caused: pasting just a cookie's
            # value (e.g. copied from devtools' "Value" column) rather than the
            # name=value pair a Cookie header needs. A value with no "=" at all
            # cannot be that pair, so catch it here instead of storing a header
            # the upstream will silently 401 on.
            return {
                "response_action": "errors",
                "errors": {
                    _BLOCK_ID: (
                        "This needs to be name=value, not just the value — see "
                        "the hint above."
                    )
                },
            }

        # The value passes through exactly once, from here into sqlite. Never
        # logged, never included in the confirmation message.
        self._store.save_mcp_header_override(
            server_name, "", {"Cookie": value}, updated_by=submitter
        )
        log.info("mcp[%s] credential updated by %s", server_name, submitter)

        if channel and thread_ts:
            await self._slack.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=(
                    f":white_check_mark: `{server_name}`'s credential was updated "
                    f"by <@{submitter}>, from the next call."
                ),
            )
        return {"response_action": "clear"}
