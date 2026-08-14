"""Convert the Markdown Claude writes into the mrkdwn Slack renders.

They are not the same language, and the differences are exactly the ones that make
a message unreadable rather than merely plain:

    Markdown            Slack mrkdwn        without conversion Slack shows
    **bold**            *bold*              literal asterisks
    *italic*            _italic_            bold, or asterisks
    ## Heading          *Heading*           a literal ##
    [text](url)         <url|text>          literal brackets and parens
    - item              • item              a dash
    ~~strike~~          ~strike~            literal tildes
    ```lang             ```                 the language name inside the block

Tables have no mrkdwn equivalent at all. They are wrapped in a code fence so the
columns keep their alignment, which is the whole reason the author drew a table.

Two things must never be transformed: the inside of a fenced code block, and the
inside an inline `code span`. Both routinely contain the characters above, and
rewriting them changes what the reader is being shown — which for a shell command
or a diff would be a lie.
"""
from __future__ import annotations

import re

_FENCE = re.compile(r"^\s*```")
_CODE_SPAN = re.compile(r"(`[^`]*`)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(?=\S)")
_LINK = re.compile(r"\[([^\]]+)\]\((\S+?)\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL)
_BOLD_ALT = re.compile(r"__(?=\S)(.+?)(?<=\S)__", re.DOTALL)
_ITALIC = re.compile(r"(?<![\*\w])\*(?=\S)([^*]+?)(?<=\S)\*(?![\*\w])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL)
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def _inline(text: str) -> str:
    """Inline conversions, applied outside code spans only."""
    parts = _CODE_SPAN.split(text)
    for i, part in enumerate(parts):
        if part.startswith("`") and part.endswith("`") and len(part) >= 2:
            continue  # a code span: leave exactly as written
        # Links first: their brackets would otherwise survive into the output.
        part = _LINK.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>", part)
        # Italic BEFORE bold. The other order looks natural and is wrong: bold
        # becomes `*x*`, which the italic rule then rewrites to `_x_`, so every
        # bold word arrived italic. The italic pattern's lookarounds already
        # refuse to match inside `**x**`, so running it first is safe.
        part = _ITALIC.sub(r"_\1_", part)
        part = _STRIKE.sub(r"~\1~", part)
        part = _BOLD.sub(r"*\1*", part)
        part = _BOLD_ALT.sub(r"*\1*", part)
        parts[i] = part
    return "".join(parts)


def _convert_prose(lines: list[str]) -> list[str]:
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]

        # A table: header, rule, then rows. Wrapped in a fence because mrkdwn has
        # no table syntax and the alignment is the point.
        if (
            _TABLE_ROW.match(line)
            and index + 1 < len(lines)
            and _TABLE_RULE.match(lines[index + 1])
        ):
            table = [line, lines[index + 1]]
            index += 2
            while index < len(lines) and _TABLE_ROW.match(lines[index]):
                table.append(lines[index])
                index += 1
            out.append("```")
            out.extend(table)
            out.append("```")
            continue

        heading = _HEADING.match(line)
        if heading:
            # mrkdwn has no headings; bold is the closest thing that still reads
            # as a heading rather than as body text.
            out.append(f"*{_inline(heading.group(2))}*" if heading.group(2) else "")
            index += 1
            continue

        line = _BULLET.sub(lambda m: f"{m.group(1)}• ", line)
        out.append(_inline(line))
        index += 1
    return out


def to_mrkdwn(text: str) -> str:
    """Convert Markdown to Slack mrkdwn, leaving code untouched."""
    if not text:
        return text

    out: list[str] = []
    prose: list[str] = []
    in_fence = False

    for line in text.splitlines():
        if _FENCE.match(line):
            if not in_fence:
                out.extend(_convert_prose(prose))
                prose = []
                in_fence = True
                # Slack shows a language tag as the first line of the block.
                out.append("```")
            else:
                in_fence = False
                out.append("```")
            continue
        if in_fence:
            out.append(line)
        else:
            prose.append(line)

    if in_fence:
        # An unterminated fence: close it, or Slack swallows the rest of the
        # message into a code block that never ends.
        out.append("```")
    out.extend(_convert_prose(prose))

    return "\n".join(out)
