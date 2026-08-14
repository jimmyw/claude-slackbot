"""Local commands: the `|`-prefixed messages the daemon answers itself.

A command is a module in this package. Dropping in a new file registers it and
makes it appear in `|help` — there is no list to keep in step.

Each module must define:

    NAME        str                     the command word, without the `|`
    ALIASES     tuple[str, ...]         optional alternative words
    SUMMARY     str                     one line, shown by |help
    build_parser() -> ArgumentParser    argparse, for arguments and usage text
    run(ctx, args) -> Awaitable[None]   what it does

argparse is used for real, which means its two habits have to be neutralised:
`error()` and `exit()` both call sys.exit and print to stderr, which in a daemon
would kill the process and say nothing to the operator. SlackParser raises
CommandError instead, and the caller turns that into a Slack reply.
"""
from __future__ import annotations

import argparse
import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

# A message whose text starts with this is a local command and is never forwarded
# to Claude. An explicit marker rather than bare keywords: `status`, `grants` and
# `revoke` are ordinary English words and collided with real requests.
COMMAND_PREFIX = "|"


class CommandError(Exception):
    """Bad input, reported to the operator rather than raised at the process."""


class CommandHelp(CommandError):
    """Requested help text. Not a failure, so it is rendered without a warning."""


class SlackParser(argparse.ArgumentParser):
    """An ArgumentParser that reports instead of exiting.

    argparse is built for command lines: it prints to stderr and calls sys.exit on
    a parse failure or `-h`. Inside a long-running daemon both are wrong, so both
    become CommandError, which the dispatcher renders into the thread.
    """

    def _print_message(self, message: str, file: Any = None) -> None:
        # argparse routes all its output through here, to stdout for help and
        # stderr for errors. In a daemon that pollutes the log and reaches nobody,
        # and `-h` would emit help twice: once printed, once via the exception.
        # Everything travels as an exception instead.
        return

    def error(self, message: str):  # noqa: ANN201
        raise CommandError(f"{message}\n\n```\n{self.format_usage().strip()}\n```")

    def exit(self, status: int = 0, message: str | None = None):  # noqa: ANN201
        # Reached by -h/--help, which argparse treats as a successful exit. That is
        # how `|grants -h` produces per-command help without a second code path.
        raise CommandHelp(message or self.format_help().strip())


@dataclass
class Context:
    """Everything a command is allowed to touch."""

    channel: str
    thread_ts: str
    user: str
    is_operator: bool
    config: Any
    store: Any
    vm: Any
    bridge: Any
    say: Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class Command:
    name: str
    aliases: tuple[str, ...]
    summary: str
    parser: argparse.ArgumentParser
    run: Callable[[Context, argparse.Namespace], Awaitable[None]]

    @property
    def usage(self) -> str:
        return self.parser.format_usage().strip().removeprefix("usage: ")


_registry: dict[str, Command] | None = None


def registry() -> dict[str, Command]:
    """Every command, keyed by name and by each alias. Discovered, not listed."""
    global _registry
    if _registry is not None:
        return _registry

    found: dict[str, Command] = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        for attr in ("NAME", "SUMMARY", "build_parser", "run"):
            if not hasattr(module, attr):
                raise RuntimeError(
                    f"command module {info.name!r} is missing {attr!r}"
                )
        command = Command(
            name=module.NAME,
            aliases=tuple(getattr(module, "ALIASES", ())),
            summary=module.SUMMARY,
            parser=module.build_parser(),
            run=module.run,
        )
        for key in (command.name, *command.aliases):
            if key in found:
                raise RuntimeError(f"two commands claim {key!r}")
            found[key] = command

    _registry = found
    return found


def commands() -> list[Command]:
    """Each command once, in a stable order, for display."""
    return sorted({c.name: c for c in registry().values()}.values(),
                  key=lambda c: c.name)


async def dispatch(ctx: Context, text: str) -> None:
    """Parse and run one `|` command. Raises CommandError for anything invalid."""
    body = text.strip()[len(COMMAND_PREFIX):].strip()
    parts = body.split()

    # A bare `|` means "what can I do?", which is the friendliest reading.
    name = parts[0].lower() if parts else "help"
    args = parts[1:]

    command = registry().get(name)
    if command is None:
        known = ", ".join(f"`{COMMAND_PREFIX}{c.name}`" for c in commands())
        raise CommandError(
            f"Unknown command `{COMMAND_PREFIX}{name}`. Available: {known}"
        )

    parsed = command.parser.parse_args(args)
    await command.run(ctx, parsed)
