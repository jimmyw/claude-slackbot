"""Markdown -> Slack mrkdwn conversion.

Slack does not speak the Markdown Claude writes, which is why messages arrived
showing literal `##`, `**` and `[text](url)`. The cases below are what actually
turns up in agent output.

The most important assertions are the negative ones: nothing inside a code fence or
an inline code span may be rewritten. Those routinely contain `*`, `_` and `[]`, and
altering them would misrepresent a command or a diff to the reader.

Run:  .venv/bin/python -m tests.test_mrkdwn
"""
from __future__ import annotations

import sys

from slackagent.mrkdwn import to_mrkdwn

failures: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got == want:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


def main() -> int:
    print("\n[1] inline emphasis")
    check("bold", to_mrkdwn("a **bold** word"), "a *bold* word")
    check("bold with underscores", to_mrkdwn("a __bold__ word"), "a *bold* word")
    check("italic", to_mrkdwn("an *italic* word"), "an _italic_ word")
    check("bold then italic", to_mrkdwn("**b** and *i*"), "*b* and _i_")
    check("strikethrough", to_mrkdwn("~~gone~~"), "~gone~")
    check("bold across words", to_mrkdwn("**two words**"), "*two words*")

    print("\n[2] headings become bold, since mrkdwn has none")
    check("h1", to_mrkdwn("# Title"), "*Title*")
    check("h2", to_mrkdwn("## Summary"), "*Summary*")
    check("h6", to_mrkdwn("###### Deep"), "*Deep*")
    check("heading with inline code",
          to_mrkdwn("## The `parse()` bug"), "*The `parse()` bug*")
    check("a hash mid-line is not a heading",
          to_mrkdwn("issue #42 is open"), "issue #42 is open")

    print("\n[3] links")
    check("link", to_mrkdwn("see [the docs](https://x.dev/a)"),
          "see <https://x.dev/a|the docs>")
    check("bare url is left alone", to_mrkdwn("https://x.dev/a"), "https://x.dev/a")

    print("\n[4] bullets")
    check("dash bullet", to_mrkdwn("- one"), "• one")
    check("star bullet", to_mrkdwn("* one"), "• one")
    check("nested keeps indent", to_mrkdwn("  - deep"), "  • deep")
    check("numbered list untouched", to_mrkdwn("1. first"), "1. first")
    check("a lone dash is not a bullet", to_mrkdwn("-"), "-")

    print("\n[5] code is never rewritten — the important part")
    check("inline code keeps asterisks",
          to_mrkdwn("run `ls **/*.c` now"), "run `ls **/*.c` now")
    check("inline code keeps brackets",
          to_mrkdwn("`arr[0](x)` is fine"), "`arr[0](x)` is fine")
    check("inline code keeps underscores",
          to_mrkdwn("`__init__` method"), "`__init__` method")
    fenced = "```\nls **/*.c\n[a](b)\n## not a heading\n```"
    check("fenced block is verbatim", to_mrkdwn(fenced), fenced)
    check("language tag is dropped",
          to_mrkdwn("```python\nx = 1\n```"), "```\nx = 1\n```")
    check("conversion resumes after a fence",
          to_mrkdwn("```\nx\n```\n**after**"), "```\nx\n```\n*after*")
    check("an unterminated fence is closed",
          to_mrkdwn("```\nx"), "```\nx\n```")

    print("\n[6] tables become code blocks so the columns still line up")
    table = (
        "| tool | asks |\n"
        "|------|------|\n"
        "| ls   | no   |\n"
        "| sudo | yes  |"
    )
    check("table is fenced", to_mrkdwn(table), "```\n" + table + "\n```")
    check("a pipe in prose is not a table",
          to_mrkdwn("use `a | b` here"), "use `a | b` here")

    print("\n[7] a realistic message survives intact")
    got = to_mrkdwn(
        "## Summary\n"
        "\n"
        "I read **three** files and found it in `src/main.c`:\n"
        "\n"
        "- the buffer is sized at compile time\n"
        "- `parse()` assumes NUL termination\n"
        "\n"
        "```c\n"
        "int x = 1;  /* **not** bold */\n"
        "```\n"
        "\n"
        "See [the docs](https://example.com)."
    )
    want = (
        "*Summary*\n"
        "\n"
        "I read *three* files and found it in `src/main.c`:\n"
        "\n"
        "• the buffer is sized at compile time\n"
        "• `parse()` assumes NUL termination\n"
        "\n"
        "```\n"
        "int x = 1;  /* **not** bold */\n"
        "```\n"
        "\n"
        "See <https://example.com|the docs>."
    )
    check("full message", got, want)

    print("\n[8] edges")
    check("empty", to_mrkdwn(""), "")
    check("newlines preserved", to_mrkdwn("a\n\nb"), "a\n\nb")
    check("unmatched asterisk left alone", to_mrkdwn("2 * 3 = 6"), "2 * 3 = 6")
    check("snake_case is not italic",
          to_mrkdwn("call do_the_thing now"), "call do_the_thing now")

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
