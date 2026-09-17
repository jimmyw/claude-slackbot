"""`|approvers` — who may press Approve/Deny besides the operator."""
from __future__ import annotations

import argparse
import datetime
import re

from .. import roles
from . import COMMAND_PREFIX, CommandError, Context, SlackParser

NAME = "approvers"
ALIASES = ("approver",)
SUMMARY = "list, add or remove the people who may approve tool calls"

# Slack sends a typed mention as markup, not as a bare id: `<@U012ABC>`, and in
# older payloads `<@U012ABC|handle>`. Accepting only the bare id would mean the
# natural way to write the command — tagging the person — silently failed.
_MENTION = re.compile(r"^<@([UW][A-Z0-9]+)(?:\|[^>]*)?>$")
_BARE = re.compile(r"^[UW][A-Z0-9]+$")

_DESCRIPTION = f"""\
Show and change who may approve the agent's tool calls.

  {COMMAND_PREFIX}approvers                who may decide, and where each one comes from
  {COMMAND_PREFIX}approvers add @person    let them press Approve/Deny from now on
  {COMMAND_PREFIX}approvers remove @person take it away again

An approver can press *Approve*, *Deny* and *Always allow*. That last one creates a
standing grant, so an approver can widen what runs unattended for everyone
afterwards — `{COMMAND_PREFIX}grants` lists those and `{COMMAND_PREFIX}revoke`
removes them, both of which stay with the operator alone. So do `{COMMAND_PREFIX}auth`,
DMs, and every other `{COMMAND_PREFIX}` command.

Approving is not the same as talking: if `ALLOWED_USERS` is set, an approver still
has to be on it to ask the agent for anything. And it is not a grant of anything the
agent could not already do — an approver decides what the agent asks about, they
never gain access themselves.

A change here is live immediately, including for an approval already waiting in a
thread. Ids named by `APPROVERS` in `daemon/.env` are a floor: they can be listed but
not removed from Slack, because a runtime list that could override the file would let
one Slack message quietly revoke what the file says.
"""

_ACTIONS = ("show", "add", "remove")


def _user(value: str) -> str:
    """A Slack user id, from a mention or from the id itself.

    Validated here so argparse renders the usage message, rather than a mistyped
    id becoming a row that can never match anyone.
    """
    text = value.strip()
    mention = _MENTION.match(text)
    if mention is not None:
        return mention.group(1)
    if _BARE.match(text) is not None:
        return text
    raise argparse.ArgumentTypeError(
        "expected a Slack user — tag them, or paste their id (U012ABCDEF) — "
        f"got {value!r}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = SlackParser(
        prog=f"{COMMAND_PREFIX}{NAME}",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f"Examples:  {COMMAND_PREFIX}approvers\n"
            f"           {COMMAND_PREFIX}approvers add @jonas\n"
            f"           {COMMAND_PREFIX}approvers remove U012ABCDEF"
        ),
    )
    parser.add_argument("action", nargs="?", default="show", choices=_ACTIONS)
    parser.add_argument(
        "who", nargs="?", type=_user,
        help="the person to add or remove: a mention, or their Slack user id",
    )
    return parser


async def run(ctx: Context, args: argparse.Namespace) -> None:
    # Belt and braces, as in |revoke: the dispatcher checks this too, but a command
    # that changes who can approve must not depend on its caller for that.
    if not ctx.is_operator:
        raise CommandError(
            f"Only <@{ctx.config.authorized_user}> can change who approves."
        )

    if args.action == "show":
        await _show(ctx)
        return

    if not args.who:
        raise CommandError(
            f"who? `{COMMAND_PREFIX}approvers {args.action} @person`, or their "
            "Slack user id."
        )

    if args.action == "add":
        await _add(ctx, args.who)
    else:
        await _remove(ctx, args.who)


async def _show(ctx: Context) -> None:
    operator = ctx.config.authorized_user
    seeded = set(ctx.config.approvers) - {operator}
    entries = {row["slack_user"]: row for row in ctx.store.approver_entries()}
    deciders = roles.load(ctx.config, ctx.store)

    lines = [
        f"*Who may approve* — {len(deciders)} "
        "can press Approve/Deny on a tool call."
    ]
    lines.append(
        f"  <@{operator}> — *operator*, from `AUTHORIZED_USER_ID`. "
        f"Also every `{COMMAND_PREFIX}` command, and DMs."
    )
    for user in sorted(seeded):
        lines.append(
            f"  <@{user}> — *approver*, from `APPROVERS` in `daemon/.env`. "
            "Remove it there and restart."
        )
    for user, row in sorted(entries.items()):
        if user == operator or user in seeded:
            # Already listed above, at the level that actually applies. A row that
            # duplicates the seed or the operator is harmless — the set is a union —
            # but listing it twice would suggest two separate grants.
            continue
        when = datetime.datetime.fromtimestamp(int(row["added_at"])).strftime(
            "%Y-%m-%d %H:%M"
        )
        by = f" by <@{row['added_by']}>" if row["added_by"] else ""
        lines.append(f"  <@{user}> — *approver*, added{by} at {when}")

    lines.append(
        f"\n`{COMMAND_PREFIX}approvers add @person` to add one, "
        f"`{COMMAND_PREFIX}approvers remove @person` to take it back. "
        f"`{COMMAND_PREFIX}approvers -h` for what an approver may and may not do."
    )
    if ctx.config.allowed_users:
        missing = sorted(
            u
            for u in deciders
            if u != operator and u not in ctx.config.allowed_users
        )
        if missing:
            # Worth saying: they can decide, but cannot ask for anything. That looks
            # like a broken bot to them and is invisible from here otherwise.
            lines.append(
                ":warning: Not on `ALLOWED_USERS`, so they can approve but cannot "
                "talk to me: " + ", ".join(f"<@{u}>" for u in missing)
            )
    await ctx.say("\n".join(lines))


async def _add(ctx: Context, user: str) -> None:
    if user == ctx.config.authorized_user:
        await ctx.say(
            f"<@{user}> is the operator, so they already approve everything. "
            "Nothing added."
        )
        return
    if user in ctx.config.approvers:
        await ctx.say(
            f"<@{user}> already approves, from `APPROVERS` in `daemon/.env`. "
            "Nothing added."
        )
        return
    if not ctx.store.add_approver(user, ctx.user):
        await ctx.say(f"<@{user}> was already an approver. Nothing changed.")
        return
    await ctx.say(
        f"<@{user}> can now approve and deny tool calls, including any approval "
        "already waiting in a thread.\n"
        "They cannot run `|` commands, cannot DM me, and gain no access of their "
        f"own — but *Always allow* is theirs to press, which creates a standing "
        f"grant (`{COMMAND_PREFIX}grants` to see them). "
        f"`{COMMAND_PREFIX}approvers remove <@{user}>` undoes this."
    )


async def _remove(ctx: Context, user: str) -> None:
    if user == ctx.config.authorized_user:
        raise CommandError(
            "The operator is always an approver. Change `AUTHORIZED_USER_ID` in "
            "`daemon/.env` if that has to move."
        )
    if user in ctx.config.approvers:
        # Refused out loud rather than deleting a row that was never there and
        # reporting success: the union means the .env seed would keep approving.
        raise CommandError(
            f"<@{user}> comes from `APPROVERS` in `daemon/.env`, which this cannot "
            "override — removing it here would report a change that did not happen. "
            "Take them out of the file and restart the daemon."
        )
    if not ctx.store.remove_approver(user):
        await ctx.say(
            f"<@{user}> was not an approver. Nothing changed — "
            f"`{COMMAND_PREFIX}approvers` lists who is."
        )
        return
    await ctx.say(
        f"<@{user}> can no longer approve or deny. Any grant they created stands "
        f"until you `{COMMAND_PREFIX}revoke` it (`{COMMAND_PREFIX}grants` shows who "
        "created each one)."
    )
