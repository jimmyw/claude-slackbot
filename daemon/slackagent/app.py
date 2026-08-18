"""Slack-facing daemon.

Runs on terra, outside the VM. Holds the Slack tokens and does the approval
check; bridges to the VM over SSH to invoke the Claude Code CLI.

Socket Mode means no inbound ports and no public endpoint — which is what makes
the "outbound only, no inbound" constraint actually hold on a home server.
"""
from __future__ import annotations

import asyncio
import logging
import re
import signal
import uuid
from collections import defaultdict

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.app.async_app import AsyncApp

from . import commands
from .approvals import ACTION_ALWAYS, ACTION_APPROVE, ACTION_DENY, ApprovalService
from .bridge import Bridge
from .config import Config, ConfigError
from .render import SILENT_MARKER, SlackRenderer
from .store import Store
from .vmctl import VmControl

log = logging.getLogger("slackagent")

_MENTION = re.compile(r"<@[A-Z0-9]+>")

COMMAND_PREFIX = commands.COMMAND_PREFIX

# Wrapped around a message that did not address the bot. Anyone may reply in a
# thread the bot owns, and most of those replies are people talking to each other
# — so the agent is asked to judge, and given a way to answer with nothing. The
# renderer turns the marker into actual silence: no placeholder, no message, no
# trace in the channel. The note is deliberately explicit that using tools also
# breaks the silence, since a run that reads ten files and then says nothing has
# still spent a minute of the thread's attention.
_UNADDRESSED_NOTE = (
    "[Daemon note, not from a person: the Slack message below was posted in a "
    "thread you are part of, but nobody mentioned you — it may well be two people "
    "talking to each other, or thinking out loud. Decide whether it is meant for "
    "you.\n"
    "If it is not meant for you, or it needs nothing from you, reply with exactly "
    f"{SILENT_MARKER} and nothing else, and use no tools. Nothing is then posted "
    "to Slack at all.\n"
    "If it is meant for you, answer it normally and do not mention this note.]\n\n"
    "{text}"
)


class Daemon:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._store = Store(config.db_path)
        self._bridge = Bridge(config)
        self._vm = VmControl(config.vm_domain, config.libvirt_uri)

        self._app = AsyncApp(token=config.bot_token)
        self._approvals = ApprovalService(config, self._store, self._app.client)

        # Learned from auth.test at startup; used to drop the duplicate `message`
        # event that accompanies an in-thread mention.
        self._bot_user_id: str | None = None

        # One lock per thread: two quick replies in the same thread must not
        # --resume the same session concurrently.
        self._thread_locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(
            asyncio.Lock
        )

        self._register_handlers()

    # -- handlers -----------------------------------------------------------

    def _register_handlers(self) -> None:
        app = self._app

        @app.event("app_mention")
        async def on_mention(event, say):  # noqa: ANN001
            log.info("app_mention in channel=%s", event.get("channel"))
            await self._on_message(event, is_mention=True)

        @app.event("message")
        async def on_message(event, say):  # noqa: ANN001
            # A direct message is unambiguously addressed to the bot, so it needs
            # no mention and starts a session like one. Without this branch DMs are
            # silently dropped: the only event that arrives is app_home_opened and
            # the bot appears dead in its own DM.
            if event.get("channel_type") == "im":
                log.info("direct message from user=%s", event.get("user"))
                await self._on_message(event, is_mention=True)
                return

            # In a channel, only threaded replies. A bare message that does not
            # mention the bot and is not in a known thread is none of our business.
            if event.get("thread_ts") is None:
                # Logged because "the bot is silent" is otherwise indistinguishable
                # from "the event never arrived", and those have completely
                # different causes. An event Slack does not deliver at all (missing
                # subscription) leaves no line here either — which is itself the
                # answer.
                log.info(
                    "ignoring channel message with no thread_ts (channel=%s type=%s)",
                    event.get("channel"), event.get("channel_type"),
                )
                return
            # A mention inside a thread fires BOTH app_mention and message.
            # app_mention already owns it; handling it here too would run the
            # turn twice.
            if self._bot_user_id and f"<@{self._bot_user_id}>" in (
                event.get("text") or ""
            ):
                return
            await self._on_message(event, is_mention=False)

        # Surfaces we acknowledge and ignore. Without listeners Bolt logs a 404
        # and a multi-line suggestion for every one, which buries real entries in
        # the only log the operator can read. The app has assistant:write, so the
        # DM pane is an Assistant surface and emits these alongside message.im.
        @app.event("app_home_opened")
        async def on_home_opened(event):  # noqa: ANN001
            return

        @app.event("assistant_thread_started")
        async def on_assistant_thread_started(event):  # noqa: ANN001
            return

        @app.event("assistant_thread_context_changed")
        async def on_assistant_context_changed(event):  # noqa: ANN001
            return

        @app.action(ACTION_APPROVE)
        async def on_approve(ack, body, action, respond):  # noqa: ANN001
            await ack()
            await self._approvals.handle_button(body, action, respond)

        @app.action(ACTION_DENY)
        async def on_deny(ack, body, action, respond):  # noqa: ANN001
            await ack()
            await self._approvals.handle_button(body, action, respond)

        @app.action(ACTION_ALWAYS)
        async def on_always(ack, body, action, respond):  # noqa: ANN001
            await ack()
            await self._approvals.handle_button(body, action, respond)

    async def _on_message(self, event: dict, *, is_mention: bool) -> None:
        if event.get("bot_id") or event.get("subtype"):
            return

        channel = event.get("channel")
        user = event.get("user")
        text = _MENTION.sub("", event.get("text") or "").strip()
        if not channel or not text:
            return

        # A new thread is rooted at the message that started it.
        thread_ts = event.get("thread_ts") or event.get("ts")

        if not is_mention:
            # In-thread reply: only continue threads we already own. This must be
            # a read, not a get-or-create — registering the thread here would
            # claim every thread anyone replies in.
            known = await asyncio.to_thread(
                self._store.find_session, channel, thread_ts
            )
            if known is None:
                log.info(
                    "ignoring reply in a thread we do not own (channel=%s thread=%s)",
                    channel, thread_ts,
                )
                return

        is_operator = user == self._config.authorized_user

        # Anyone in a channel the bot was invited to may talk: the invite is the
        # grant. A DM is not such a channel — any workspace member can open one —
        # so DMs stay with the operator, or the audience would be the whole
        # workspace rather than the people deliberately given access.
        if event.get("channel_type") == "im" and not is_operator:
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=(
                    ":no_entry: I only take direct messages from "
                    f"<@{self._config.authorized_user}>. Mention me in a channel "
                    "we are both in instead."
                ),
            )
            log.warning("refused DM from non-operator %s", user)
            return

        # An explicit allowlist, when configured, narrows it further.
        if (
            self._config.allowed_users
            and not is_operator
            and user not in self._config.allowed_users
        ):
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=(
                    ":no_entry: You are not on this agent's allowlist. Ask "
                    f"<@{self._config.authorized_user}> to add you."
                ),
            )
            log.warning("refused message from %s (not on ALLOWED_USERS)", user)
            return

        # Daemon commands are handled here and never reach Claude. The match must
        # be tight: these are ordinary words, and a message that merely begins with
        # one is far more likely to be a request than a command.
        if await self._handle_command(channel, thread_ts, text, user or "", is_operator):
            return

        lock = self._thread_locks[(channel, thread_ts)]
        async with lock:
            await self._run_turn(
                channel, thread_ts, text, user or "", addressed=is_mention
            )

    # -- the turn -----------------------------------------------------------

    async def _run_turn(
        self,
        channel: str,
        thread_ts: str,
        prompt: str,
        requested_by: str = "",
        *,
        addressed: bool = True,
    ) -> None:
        session = await asyncio.to_thread(
            self._store.get_or_create_session, channel, thread_ts, str(uuid.uuid4())
        )

        # A mention or a DM is unambiguously for the bot, so it answers and shows
        # its working. Anything else it has to judge for itself, and until it has,
        # nothing is posted.
        renderer = SlackRenderer(
            self._app.client,
            channel,
            thread_ts,
            update_interval_s=self._config.update_interval_s,
            quiet=not addressed,
        )
        if not addressed:
            prompt = _UNADDRESSED_NOTE.format(text=prompt)
        await renderer.start()

        if not await self._vm.is_running():
            await renderer.fail(
                f"The agent VM (`{self._config.vm_domain}`) is not running. "
                "Starting it…"
            )
            if not await self._vm.start():
                await renderer.fail("Could not start the VM. Check `virsh` on terra.")
                return
            await asyncio.sleep(20)

        run_token = uuid.uuid4().hex
        self._approvals.register_run(
            run_token, channel, thread_ts, session.session_id, requested_by
        )
        if requested_by and requested_by != self._config.authorized_user:
            log.info("run requested by guest %s in %s", requested_by, channel)

        # The session exists on disk from the `init` event onward, and `claude`
        # refuses --session-id for an id that already exists. So the moment we see
        # init, this thread must switch to --resume — even if the run then dies.
        # Keying off the `result` event instead would leave a crashed first run
        # retrying --session-id forever and permanently break the thread.
        session_created = False

        # Read per run so |auth takes effect on the next message, not the next
        # restart. The .env value is only the default.
        policy = await asyncio.to_thread(
            self._store.get_setting, "agent_policy", self._config.agent_policy
        )

        try:
            async for event in self._bridge.run(
                prompt=prompt,
                session_id=session.session_id,
                resume=not session.is_new,
                run_token=run_token,
                policy=policy,
            ):
                if (
                    event.get("type") == "system"
                    and event.get("subtype") == "init"
                    and not session_created
                ):
                    session_created = True
                    await asyncio.to_thread(
                        self._store.mark_session_created, channel, thread_ts
                    )
                await renderer.handle(event)
        except Exception:
            log.exception("run failed")
            await renderer.fail("The run failed. Check the daemon log on terra.")
        finally:
            self._approvals.unregister_run(run_token)
            await renderer.flush(force=True)

    async def _handle_command(
        self, channel: str, thread_ts: str, text: str, user: str, is_operator: bool
    ) -> bool:
        """Handle a local command. Returns True if the message was one.

        Three rules, in order:

          1. If ANY line starts with `|`, the message is never forwarded to Claude
             — not even when it fails to parse. A stray `|whatever` is a mistyped
             command, and sending it on would leak an operator instruction into the
             conversation the agent sees.
          2. Only the operator may run them.
          3. The command must be the whole message, on one line.

        Parsing itself lives in slackagent.commands: one module per command,
        discovered rather than listed, using argparse so `|grants -h` works.
        """
        lines = text.strip().splitlines()
        if not any(line.strip().startswith(COMMAND_PREFIX) for line in lines):
            return False

        # Past this point the message is consumed no matter what.
        async def say(message: str) -> None:
            await self._say(channel, thread_ts, message)

        if not is_operator:
            await say(
                f":no_entry: `{COMMAND_PREFIX}` commands are only for "
                f"<@{self._config.authorized_user}>. Ask me something instead and "
                "I will do it, subject to their approval."
            )
            log.info("refused local command from guest %s", user)
            return True

        stripped = text.strip()
        if len(lines) > 1 or not stripped.startswith(COMMAND_PREFIX):
            await say(
                f"A `{COMMAND_PREFIX}` command has to be the whole message, on its "
                f"own line. Nothing was sent to Claude. Try `{COMMAND_PREFIX}help`."
            )
            return True

        ctx = commands.Context(
            channel=channel, thread_ts=thread_ts, user=user,
            is_operator=is_operator, config=self._config, store=self._store,
            vm=self._vm, bridge=self._bridge, approvals=self._approvals,
            say=say,
        )
        try:
            await commands.dispatch(ctx, stripped)
        except commands.CommandHelp as help_text:
            # -h and --help arrive here: usage, not a failure.
            await say(f"```\n{help_text}\n```")
        except commands.CommandError as exc:
            await say(f":warning: {exc}\nNothing was sent to Claude.")
        except Exception:
            log.exception("local command failed: %r", stripped)
            await say(
                ":warning: That command failed. Nothing was sent to Claude; "
                "see the daemon log."
            )
        return True

    async def _say(self, channel: str, thread_ts: str, text: str) -> None:
        await self._app.client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text=text
        )

    # -- lifecycle ----------------------------------------------------------

    async def run(self) -> None:
        await self._approvals.start()

        try:
            identity = await self._app.client.auth_test()
            self._bot_user_id = identity.get("user_id")
            log.info("authenticated as %s (%s)", identity.get("user"), self._bot_user_id)
        except Exception:
            # Not fatal: without it, an in-thread mention is handled twice. Warn
            # loudly rather than refusing to start.
            log.warning(
                "auth.test failed; in-thread mentions may be processed twice",
                exc_info=True,
            )

        handler = AsyncSocketModeHandler(self._app, self._config.app_token)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        log.info("connecting to Slack (socket mode)")
        await handler.connect_async()
        log.info("ready")

        try:
            await stop.wait()
        finally:
            log.info("shutting down")
            await handler.disconnect_async()
            await self._approvals.stop()
            self._store.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = Config.from_env()
        config.validate()
    except ConfigError as exc:
        raise SystemExit(f"configuration error: {exc}") from exc

    asyncio.run(Daemon(config).run())


if __name__ == "__main__":
    main()
