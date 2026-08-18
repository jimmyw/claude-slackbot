"""The daemon's half of "don't reply when you weren't asked".

Two ways a message goes unanswered, and this covers the daemon's side of both:

  * the agent judges an unaddressed message was not for it (test_render.py covers
    what the renderer then does with the marker; here it is the wiring that
    decides a message needs judging at all, and what the agent is told);
  * the operator paused the thread with |pause, which is not a judgement at all —
    nothing is forwarded and nothing is posted until |resume.

Run:  .venv/bin/python -m tests.test_silence
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from slackagent.render import SILENT_MARKER

# Reused rather than duplicated: this is the same fake-Slack, fake-config Daemon.
from tests.test_commands import AUTHORIZED, GUEST, make_daemon

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


class FakeBridge:
    """Records the prompt it was given and replays a scripted reply."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def run(self, *, prompt: str, session_id: str, **_kwargs):
        self.prompts.append(prompt)
        yield {"type": "system", "subtype": "init", "session_id": session_id}
        yield {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": self.reply}]},
        }
        yield {"type": "result", "duration_ms": 500, "num_turns": 1}
        yield {"type": "_bridge", "exit_code": 0, "stderr": ""}


async def turn(daemon, reply: str, *, is_mention: bool, text: str = "shall we ship?"):
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
            "channel_type": "channel",
        },
        is_mention=is_mention,
    )
    return bridge, daemon._app.client.posted


async def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        daemon = make_daemon(Path(raw))
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
        check("the prompt is verbatim", bridge.prompts[-1] == "shall we ship?",
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
              daemon._store.is_thread_paused("C1", "1.1"))  # noqa: SLF001

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
        check("|resume is answered while paused", "Answering here again" in reply,
              reply[:80])
        check("the pause is gone",
              not daemon._store.is_thread_paused("C1", "1.1"))  # noqa: SLF001
        reply = await command("|resume")
        check("resuming an unpaused thread changes nothing",
              "was not paused" in reply, reply[:60])

        bridge, posted = await turn(daemon, "Back.", is_mention=True)
        check("the thread answers again after |resume",
              bridge.prompts and posted, (bridge.prompts, posted))

        # A pause is per thread: another thread in the same channel is unaffected.
        daemon._store.pause_thread("C1", "1.1", AUTHORIZED)  # noqa: SLF001
        check("a different thread is not paused",
              not daemon._store.is_thread_paused("C1", "9.9"))  # noqa: SLF001
        daemon._store.resume_thread("C1", "1.1")  # noqa: SLF001

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
