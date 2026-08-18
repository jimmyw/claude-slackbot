"""The daemon's half of "don't reply when you weren't asked".

test_render.py covers what the renderer does with the marker; this covers the
wiring that decides a message needs judging at all: which turns are quiet, and
what the agent is actually told about the message it was handed.

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

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
