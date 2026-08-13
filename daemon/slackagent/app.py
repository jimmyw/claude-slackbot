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

from .approvals import ACTION_APPROVE, ACTION_DENY, ApprovalService
from .bridge import Bridge
from .config import Config, ConfigError
from .render import SlackRenderer
from .store import Store
from .vmctl import VmControl

log = logging.getLogger("slackagent")

_MENTION = re.compile(r"<@[A-Z0-9]+>")


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
            await self._on_message(event, is_mention=True)

        @app.event("message")
        async def on_message(event, say):  # noqa: ANN001
            # Only threaded replies; a bare channel message that does not mention
            # the bot and is not in a known thread is none of our business.
            if event.get("thread_ts") is None:
                return
            # A mention inside a thread fires BOTH app_mention and message.
            # app_mention already owns it; handling it here too would run the
            # turn twice.
            if self._bot_user_id and f"<@{self._bot_user_id}>" in (
                event.get("text") or ""
            ):
                return
            await self._on_message(event, is_mention=False)

        @app.action(ACTION_APPROVE)
        async def on_approve(ack, body, action, respond):  # noqa: ANN001
            await ack()
            await self._approvals.handle_button(body, action, respond)

        @app.action(ACTION_DENY)
        async def on_deny(ack, body, action, respond):  # noqa: ANN001
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
                return

        if user != self._config.authorized_user:
            await self._app.client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=(
                    ":no_entry: Only the authorized operator can drive this agent."
                ),
            )
            log.warning("ignored message from unauthorized user %s", user)
            return

        if text.lower() in {"status", "!status"}:
            await self._post_status(channel, thread_ts)
            return

        lock = self._thread_locks[(channel, thread_ts)]
        async with lock:
            await self._run_turn(channel, thread_ts, text)

    # -- the turn -----------------------------------------------------------

    async def _run_turn(self, channel: str, thread_ts: str, prompt: str) -> None:
        session = await asyncio.to_thread(
            self._store.get_or_create_session, channel, thread_ts, str(uuid.uuid4())
        )

        renderer = SlackRenderer(
            self._app.client,
            channel,
            thread_ts,
            update_interval_s=self._config.update_interval_s,
        )
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
        self._approvals.register_run(run_token, channel, thread_ts, session.session_id)

        # Only a completed turn makes the session resumable. If the first run dies
        # before the CLI writes its transcript, counting it would make the next
        # message --resume a session that never existed.
        completed = False

        try:
            async for event in self._bridge.run(
                prompt=prompt,
                session_id=session.session_id,
                resume=not session.is_new,
                run_token=run_token,
            ):
                if event.get("type") == "result":
                    completed = True
                await renderer.handle(event)
        except Exception:
            log.exception("run failed")
            await renderer.fail("The run failed. Check the daemon log on terra.")
        finally:
            self._approvals.unregister_run(run_token)
            await renderer.flush(force=True)
            if completed:
                await asyncio.to_thread(self._store.record_turn, channel, thread_ts)

    async def _post_status(self, channel: str, thread_ts: str) -> None:
        state = await self._vm.state()
        ip = await self._vm.ip_address()
        probe = await self._bridge.probe()
        # agent-exec exits 64 on an empty job, which means SSH authenticated and
        # the forced command ran — a healthy path.
        ssh_ok = "reachable" if probe.exit_code in {0, 64} else "unreachable"

        await self._app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=(
                f"VM `{self._config.vm_domain}`: {state}"
                f"{f' at {ip}' if ip else ''}\nSSH bridge: {ssh_ok}"
            ),
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
