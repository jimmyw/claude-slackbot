"""Renderer test, driven by real stream-json captured from `claude -p`.

Fixtures in tests/fixtures/*.jsonl are verbatim output from Claude Code 2.1.231,
so this exercises the actual event shapes rather than shapes we assumed.

Run:  .venv/bin/python -m tests.test_render
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from slackagent.render import (
    SILENT_MARKER,
    SlackRenderer,
    _chunk,
    _describe_tool,
    _result_footer,
)

FIXTURES = Path(__file__).parent / "fixtures"

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


class FakeSlack:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.posts: list[dict] = []
        self.deletes: list[dict] = []

    async def chat_postMessage(self, **kwargs):  # noqa: N802
        self.posts.append(kwargs)
        return {"ts": "1700000000.000100", "ok": True}

    async def chat_update(self, **kwargs):  # noqa: N802
        self.updates.append(kwargs)
        return {"ok": True}

    async def chat_delete(self, **kwargs):  # noqa: N802
        self.deletes.append(kwargs)
        return {"ok": True}


async def replay(name: str) -> tuple[FakeSlack, SlackRenderer]:
    slack = FakeSlack()
    renderer = SlackRenderer(slack, "C1", "1.1", update_interval_s=0.0)
    await renderer.start()
    for line in (FIXTURES / name).read_text().splitlines():
        line = line.strip()
        if line:
            await renderer.handle(json.loads(line))
    return slack, renderer


async def test_real_streams() -> None:
    print("\n[1] replaying real captured streams")
    for name, expected in [
        ("simple_reply.jsonl", "OK"),
        ("resumed_reply.jsonl", "OK"),
        ("tool_use.jsonl", "MAGICVALUE=42"),
    ]:
        slack, _ = await replay(name)
        check(f"{name}: produced at least one update", len(slack.updates) > 0)
        final = slack.updates[-1]
        check(
            f"{name}: answer reached Slack",
            expected in final["text"],
            final["text"][:120],
        )
        check(
            f"{name}: every block is within Slack's 3000-char limit",
            all(
                len(b.get("text", {}).get("text", "")) <= 3000
                for b in final["blocks"]
                if b.get("type") == "section"
            ),
        )
        check(f"{name}: at most 50 blocks", len(final["blocks"]) <= 50)

    slack, _ = await replay("tool_use.jsonl")
    contexts = [
        e["text"]
        for b in slack.updates[-1]["blocks"]
        if b.get("type") == "context"
        for e in b["elements"]
    ]
    check(
        "tool_use: the Read call is shown as activity",
        any("Read(" in c for c in contexts),
        contexts,
    )
    check(
        "tool_use: footer carries cost and duration",
        any("$" in c and "s" in c for c in contexts),
        contexts,
    )


async def test_error_paths() -> None:
    print("\n[2] error and transport paths")

    slack = FakeSlack()
    renderer = SlackRenderer(slack, "C1", "1.1", update_interval_s=0.0)
    await renderer.start()
    await renderer.handle({"type": "_bridge", "exit_code": 255,
                           "stderr": "ssh: connect to host 1.2.3.4 port 22: No route to host"})
    check("ssh failure surfaces to the user", "unexpectedly" in slack.updates[-1]["text"])
    check("stderr tail is included", "No route to host" in str(slack.updates[-1]["blocks"]))

    slack = FakeSlack()
    renderer = SlackRenderer(slack, "C1", "1.1", update_interval_s=0.0)
    await renderer.start()
    await renderer.handle({"type": "result", "subtype": "error", "is_error": True,
                           "result": "session limit reached", "duration_ms": 1200})
    check("run error surfaces to the user", "session limit reached" in slack.updates[-1]["text"])

    # Captured from a real unauthenticated run through the SSH bridge. Note its
    # subtype is "success" even though is_error is true, so keying off subtype
    # alone would show the user a blank reply.
    slack, _ = await replay("not_logged_in.jsonl")
    final = slack.updates[-1]
    check("unauthenticated run tells the user to log in",
          "Not logged in" in final["text"], final["text"][:120])
    check("and is marked as a warning", ":warning:" in final["text"],
          final["text"][:120])

    slack = FakeSlack()
    renderer = SlackRenderer(slack, "C1", "1.1", update_interval_s=0.0)
    await renderer.start()
    # Unknown event types must not raise — the stream is free to grow new ones.
    for event in [
        {"type": "rate_limit_event", "foo": 1},
        {"type": "something_new_in_a_later_release"},
        {"type": "assistant"},
        {"type": "assistant", "message": {}},
        {"type": "assistant", "message": {"content": None}},
    ]:
        await renderer.handle(event)
    check("unknown and malformed events are tolerated", True)


async def test_silence() -> None:
    """A message that was not addressed to the bot may end in nothing at all."""
    print("\n[5] staying silent when the message was not for us")

    async def run(text: str, *, quiet: bool) -> FakeSlack:
        slack = FakeSlack()
        renderer = SlackRenderer(
            slack, "C1", "1.1", update_interval_s=0.0, quiet=quiet
        )
        await renderer.start()
        await renderer.handle(
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": text}]}}
        )
        await renderer.handle({"type": "result", "duration_ms": 900,
                               "total_cost_usd": 0.01, "num_turns": 1})
        return slack

    slack = await run(SILENT_MARKER, quiet=True)
    check("quiet mode posts no placeholder", slack.posts == [], slack.posts)
    check("the marker posts nothing at all",
          slack.updates == [] and slack.posts == [], slack.updates)
    check("and nothing is deleted, because nothing existed",
          slack.deletes == [], slack.deletes)

    # Tool activity must not break the silence on its own: the agent may still
    # decide the message was not for it, and a bare 🔧 line would be a reply.
    slack = FakeSlack()
    renderer = SlackRenderer(slack, "C1", "1.1", update_interval_s=0.0, quiet=True)
    await renderer.start()
    await renderer.handle({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}}]}})
    check("tool activity alone stays quiet", slack.posts == [], slack.posts)
    await renderer.handle({"type": "assistant", "message": {"content": [
        {"type": "text", "text": SILENT_MARKER}]}})
    await renderer.handle({"type": "result", "duration_ms": 1})
    check("a marker after a tool call still posts nothing",
          slack.posts == [] and slack.updates == [], slack.posts)

    # An addressed run already posted "working…", so silence has to retract it.
    slack = await run(SILENT_MARKER, quiet=False)
    check("an addressed run posts its placeholder", len(slack.posts) == 1, slack.posts)
    check("the marker deletes the placeholder", len(slack.deletes) == 1, slack.deletes)
    check("the placeholder is never updated into a reply",
          slack.updates == [], slack.updates)

    for variant in ("[[NO-REPLY]]", "[[ no_reply ]]", "\n[[no reply]]\n"):
        slack = await run(variant, quiet=True)
        check(f"marker variant {variant!r} is honoured",
              slack.posts == [] and slack.updates == [], slack.posts)

    # Mixed with prose it is a slip, not a decision: the prose is the answer.
    slack = await run(f"{SILENT_MARKER}\n\nActually, the build is green.", quiet=True)
    check("prose beside the marker is posted", len(slack.posts) == 1, slack.posts)
    posted = slack.posts[-1]
    check("the answer survives", "build is green" in posted["text"], posted["text"])
    check("the marker never reaches Slack",
          "no-reply" not in str(posted).lower(), posted["text"])

    # A real answer in quiet mode posts once and then edits, with no placeholder.
    slack = await run("The tests pass.", quiet=True)
    check("a real answer breaks the silence", len(slack.posts) == 1, slack.posts)
    check("the first post carries the answer, not a placeholder",
          "tests pass" in slack.posts[0]["text"], slack.posts[0]["text"])
    check("later events edit that message", len(slack.updates) >= 1, slack.updates)

    # Errors are still reported: silence there would hide a broken daemon.
    slack = FakeSlack()
    renderer = SlackRenderer(slack, "C1", "1.1", update_interval_s=0.0, quiet=True)
    await renderer.start()
    await renderer.handle({"type": "_bridge", "exit_code": 255, "stderr": "no route"})
    check("a transport failure is reported even in quiet mode",
          len(slack.posts) == 1 and "unexpectedly" in slack.posts[0]["text"],
          slack.posts)


async def test_long_output() -> None:
    print("\n[3] long output stays inside Slack's limits")
    slack = FakeSlack()
    renderer = SlackRenderer(slack, "C1", "1.1", update_interval_s=0.0)
    await renderer.start()
    paragraph = ("word " * 200).strip()
    await renderer.handle(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text",
                                     "text": "\n\n".join([paragraph] * 40)}]},
        }
    )
    await renderer.flush(force=True)
    final = slack.updates[-1]
    check("blocks capped at 50", len(final["blocks"]) <= 50, len(final["blocks"]))
    check(
        "no section block exceeds 3000 chars",
        all(
            len(b.get("text", {}).get("text", "")) <= 3000
            for b in final["blocks"]
            if b.get("type") == "section"
        ),
    )
    check("fallback text within limit", len(final["text"]) <= 3000, len(final["text"]))

    print("\n[3b] markdown becomes mrkdwn, and code survives")
    slack = FakeSlack()
    renderer = SlackRenderer(slack, "C1", "1.1", update_interval_s=0.0)
    await renderer.start()
    await renderer.handle({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text":
            "## Done\n\nRead **three** files.\n\n- one\n- two\n\n"
            "```c\nint x = 1;  /* **kept** */\n```\n\n"
            "See [docs](https://x.dev)."}]},
    })
    await renderer.flush(force=True)
    sent = slack.updates[-1]
    body = "\n".join(
        b["text"]["text"] for b in sent["blocks"] if b.get("type") == "section"
    )
    check("heading became bold", "*Done*" in body, body[:60])
    check("no literal ## reaches Slack", "## " not in body, body[:60])
    check("bold converted", "*three*" in body and "**three**" not in body, body[:80])
    check("bullets became •", "• one" in body, body[:120])
    check("link converted", "<https://x.dev|docs>" in body, body[-60:])
    check("code block content untouched", "/* **kept** */" in body, body)
    check("language tag dropped", "```c" not in body, body)
    check("the fallback text is converted too",
          "*Done*" in sent["text"] and "## " not in sent["text"], sent["text"][:60])

    print("\n[3c] a long message never splits a code fence")
    slack = FakeSlack()
    renderer = SlackRenderer(slack, "C1", "1.1", update_interval_s=0.0)
    await renderer.start()
    big_code = "\n".join(f"line {i:04d} of output" for i in range(300))
    await renderer.handle({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text":
            "Here it is:\n\n```\n" + big_code + "\n```\n\nDone."}]},
    })
    await renderer.flush(force=True)
    sections = [
        b["text"]["text"] for b in slack.updates[-1]["blocks"]
        if b.get("type") == "section"
    ]
    check("it did split into several blocks", len(sections) > 1, len(sections))
    for i, chunk in enumerate(sections):
        check(f"block {i} has balanced fences", chunk.count("```") % 2 == 0,
              chunk.count("```"))

    print("\n[4] helpers")
    check("chunk never exceeds the size", all(len(c) <= 100 for c in _chunk("x" * 950, 100)))
    check("chunk preserves everything",
          "".join(_chunk("a" * 500, 100)).replace("\n", "") == "a" * 500)
    check("tool description shows the path",
          _describe_tool("Read", {"file_path": "/home/agent/x.py"}) == "Read(/home/agent/x.py)")
    check("tool description tolerates a non-dict input",
          _describe_tool("Bash", None) == "Bash")
    check("footer reports denials",
          "1 denied (Write)" in _result_footer(
              {"permission_denials": [{"tool_name": "Write"}]}))
    check("footer on an empty result is empty", _result_footer({}) == "")


async def main() -> int:
    await test_real_streams()
    await test_error_paths()
    await test_long_output()
    await test_silence()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
