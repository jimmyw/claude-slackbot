"""The daemon's half of "don't reply when you weren't asked".

Two ways a message goes unanswered, and this covers the daemon's side of both:

  * the agent judges an unaddressed message was not for it (test_render.py covers
    what the renderer then does with the marker; here it is the wiring that
    decides a message needs judging at all, and what the agent is told);
  * the operator muted the thread — |pause forwards nothing at all, |silent
    forwards only what actually tagged the bot. Neither is a judgement, and both
    are decided out here, so an ignored message costs nothing.

Run:  .venv/bin/python -m tests.test_silence
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from slackagent.render import SILENT_MARKER
from slackagent.store import MODE_ACTIVE, MODE_PAUSED, MODE_SILENT

# Reused rather than duplicated: this is the same fake-Slack, fake-config Daemon.
from tests.test_commands import AUTHORIZED, GUEST, make_daemon

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


class FakeProbe:
    exit_code = 64  # what agent-exec returns for an empty job: a healthy path
    stderr = ""


class FakeBridge:
    """Records the prompt it was given and replays a scripted reply."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.system_appends: list[str] = []

    async def probe(self) -> FakeProbe:
        return FakeProbe()

    async def run(self, *, prompt: str, session_id: str, system_append: str = "",
                  **_kwargs):
        self.prompts.append(prompt)
        self.system_appends.append(system_append)
        yield {"type": "system", "subtype": "init", "session_id": session_id}
        yield {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": self.reply}]},
        }
        yield {"type": "result", "duration_ms": 500, "num_turns": 1}
        yield {"type": "_bridge", "exit_code": 0, "stderr": ""}


async def turn(
    daemon,
    reply: str,
    *,
    is_mention: bool,
    text: str = "shall we ship?",
    channel_type: str = "channel",
):
    bridge = FakeBridge(reply)
    daemon._bridge = bridge  # noqa: SLF001
    daemon._vm.is_running = lambda: asyncio.sleep(0, result=True)  # noqa: SLF001
    daemon._app.client.posted.clear()
    await daemon._on_message(  # noqa: SLF001
        {
            "channel": "C1",
            "thread_ts": "1.1",
            "ts": "2.2",
            "text": text,
            "user": AUTHORIZED,
            "channel_type": channel_type,
        },
        is_mention=is_mention,
    )
    return bridge, daemon._app.client.posted


async def main() -> int:
    with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as raw2:
        tmp2 = Path(raw2)
        daemon = make_daemon(Path(raw))
        daemon._bot_user_id = "U_BOT"  # noqa: SLF001  as auth.test would set it
        # |status asks the VM and the bridge; neither is real here.
        daemon._vm.state = lambda: asyncio.sleep(0, result="running")  # noqa: SLF001
        daemon._vm.ip_address = lambda: asyncio.sleep(0, result=None)  # noqa: SLF001
        daemon._bridge = FakeBridge("")  # noqa: SLF001
        # The thread has to be one we own, or the reply is dropped before any of
        # this is reached — that path is the pre-existing behaviour.
        daemon._store.get_or_create_session("C1", "1.1", "11111111-1111-1111-1111-111111111111")  # noqa: SLF001

        print("\n[1] an unaddressed reply is judged, and can end in silence")
        bridge, posted = await turn(daemon, SILENT_MARKER, is_mention=False)
        prompt = bridge.prompts[-1]
        check("the agent is told nobody mentioned it", "nobody mentioned you" in prompt,
              prompt[:80])
        check("it is told how to say nothing", SILENT_MARKER in prompt, prompt[:80])
        check("the original message is passed through", "shall we ship?" in prompt,
              prompt[-60:])
        check("nothing at all is posted to Slack", posted == [], posted)

        print("\n[2] the same reply, when it is for us, is answered normally")
        bridge, posted = await turn(daemon, "Yes — the tests are green.", is_mention=False)
        check("a real answer is posted", len(posted) == 1, posted)
        check("and it carries the answer", "tests are green" in posted[0]["text"],
              posted and posted[0]["text"])

        print("\n[3] a mention is never wrapped and never deferred")
        bridge, posted = await turn(daemon, "On it.", is_mention=True)
        check("the prompt is the message, labelled with who wrote it, and nothing "
              "else — no unaddressed note",
              bridge.prompts[-1] == f"<@{AUTHORIZED}>: shall we ship?",
              bridge.prompts[-1][:80])
        check("the placeholder goes up immediately",
              posted and posted[0]["text"] == "_working…_", posted)

        print("\n[4] |pause stops the thread outright; |resume lifts it")
        client = daemon._app.client  # noqa: SLF001

        async def command(text: str) -> str:
            client.posted.clear()
            handled = await daemon._handle_command(  # noqa: SLF001
                "C1", "1.1", text, AUTHORIZED, True
            )
            check(f"{text!r} was handled locally", handled, text)
            return client.posted[-1]["text"] if client.posted else ""

        reply = await command("|pause")
        check("|pause confirms and names the way out",
              "Paused" in reply and "|resume" in reply, reply[:90])
        check("the pause is recorded",
              daemon._store.thread_mode("C1", "1.1") == MODE_PAUSED)  # noqa: SLF001

        bridge, posted = await turn(daemon, "Yes.", is_mention=True)
        check("a mention in a paused thread starts no run", bridge.prompts == [],
              bridge.prompts)
        check("and posts nothing at all", posted == [], posted)
        bridge, posted = await turn(daemon, "Yes.", is_mention=False)
        check("nor does an in-thread reply", bridge.prompts == [] and posted == [],
              (bridge.prompts, posted))

        reply = await command("|pause")
        check("pausing twice says so", "Already paused" in reply, reply[:60])

        # The one thing that must still work while paused, or there is no way out.
        reply = await command("|resume")
        check("|resume is answered while paused",
              "Answering here normally again" in reply and "Paused" in reply,
              reply[:100])
        check("the pause is gone",
              daemon._store.thread_mode("C1", "1.1") == MODE_ACTIVE)  # noqa: SLF001
        reply = await command("|resume")
        check("resuming an unmuted thread changes nothing",
              "already answering normally" in reply, reply[:80])

        bridge, posted = await turn(daemon, "Back.", is_mention=True)
        check("the thread answers again after |resume",
              bridge.prompts and posted, (bridge.prompts, posted))

        # A pause is per thread: another thread in the same channel is unaffected.
        daemon._store.set_thread_mode(  # noqa: SLF001
            "C1", "1.1", MODE_PAUSED, AUTHORIZED)
        check("a different thread is not paused",
              daemon._store.thread_mode("C1", "9.9") == MODE_ACTIVE)  # noqa: SLF001
        daemon._store.set_thread_mode(  # noqa: SLF001
            "C1", "1.1", MODE_ACTIVE, AUTHORIZED)

        print("\n[5] |silent forwards only what tagged us")
        reply = await command("|silent")
        check("|silent confirms and names the way out",
              "ention-only" in reply and "|resume" in reply, reply[:100])
        check("the mode is recorded",
              daemon._store.thread_mode("C1", "1.1") == MODE_SILENT)  # noqa: SLF001

        bridge, posted = await turn(daemon, "Yes.", is_mention=False)
        # The prompts assertion is the one that matters: the [[no-reply]] tests prove
        # only that nothing was POSTED, and an implementation that still spends a
        # turn would pass those while costing money on every message.
        check("an untagged reply starts no run at all", bridge.prompts == [],
              bridge.prompts)
        check("and posts nothing", posted == [], posted)

        bridge, posted = await turn(daemon, "On it.", is_mention=True)
        check("a mention is forwarded and answered",
              bridge.prompts and posted, (bridge.prompts, posted))

        bridge, posted = await turn(daemon, "Sure.", is_mention=True,
                                    channel_type="im")
        check("so is a DM", bridge.prompts and posted, (bridge.prompts, posted))

        entry = daemon._store.thread_mode_entry("C1", "1.1")  # noqa: SLF001
        check("dropped messages are counted for |status",
              entry is not None and entry.dropped == 1, entry)

        reply = await command("|status")
        check("|status says mention-only", "mention-only" in reply, reply[:200])

        print("\n[6] the modes convert into each other, and say so")
        reply = await command("|pause")
        check("|pause from silent says a tag no longer works",
              "no longer" in reply, reply[:120])
        bridge, _ = await turn(daemon, "hi", is_mention=True)
        check("and a mention really is dropped now", bridge.prompts == [],
              bridge.prompts)

        reply = await command("|silent")
        check("|silent from paused announces that it LOOSENED the mute",
              "Lifted the pause" in reply, reply[:140])
        bridge, _ = await turn(daemon, "hi", is_mention=True)
        check("a mention works again", bridge.prompts != [], bridge.prompts)

        reply = await command("|resume")
        check("|resume names mention-only, not paused",
              "Mention-only" in reply and "Paused" not in reply, reply[:160])
        check("and the thread is active",
              daemon._store.thread_mode("C1", "1.1") == MODE_ACTIVE)  # noqa: SLF001

        print("\n[7] a muted thread does not post refusals")
        # These refusals used to run BEFORE the mode check, so a paused thread
        # answered anyone not on the allowlist with a public :no_entry:.
        guarded = make_daemon(tmp2, allowed_users=frozenset({AUTHORIZED}))
        guarded._store.get_or_create_session(  # noqa: SLF001
            "C1", "1.1", "22222222-2222-2222-2222-222222222222")
        guarded._store.set_thread_mode(  # noqa: SLF001
            "C1", "1.1", MODE_PAUSED, AUTHORIZED)
        guarded._bridge = FakeBridge("hi")  # noqa: SLF001
        guarded._vm.is_running = lambda: asyncio.sleep(0, result=True)  # noqa: SLF001
        guarded._app.client.posted.clear()  # noqa: SLF001
        await guarded._on_message(  # noqa: SLF001
            {"channel": "C1", "thread_ts": "1.1", "ts": "3.3", "text": "hello",
             "user": GUEST, "channel_type": "channel"},
            is_mention=False,
        )
        check("a non-allowlisted guest in a paused thread gets no reply",
              guarded._app.client.posted == [],  # noqa: SLF001
              guarded._app.client.posted)  # noqa: SLF001

        print("\n[8] a bare mention still starts a turn")
        # "@bot" with nothing else used to be dropped before any of the above,
        # because stripping the mention left an empty string.
        daemon._bridge = bridge = FakeBridge("Yes?")  # noqa: SLF001
        daemon._app.client.posted.clear()  # noqa: SLF001
        await daemon._on_message(  # noqa: SLF001
            {"channel": "C1", "thread_ts": "1.1", "ts": "4.4",
             "text": "<@U_BOT>", "user": AUTHORIZED, "channel_type": "channel"},
            is_mention=True,
        )
        check("a message that is only our mention is forwarded",
              bridge.prompts != [], bridge.prompts)
        check("with a prompt saying so",
              bridge.prompts and "no other text" in bridge.prompts[0],
              bridge.prompts)

        print("\n[9] other people's mentions survive; ours does not")
        daemon._bridge = bridge = FakeBridge("ok")  # noqa: SLF001
        await daemon._on_message(  # noqa: SLF001
            {"channel": "C1", "thread_ts": "1.1", "ts": "5.5",
             "text": "<@U_BOT> ask <@U_BOB> about the meter",
             "user": AUTHORIZED, "channel_type": "channel"},
            is_mention=True,
        )
        prompt = bridge.prompts[-1]
        check("our own mention is stripped", "<@U_BOT>" not in prompt, prompt)
        check("a third party's mention is kept — that is who is being talked about",
              "<@U_BOB>" in prompt, prompt)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
