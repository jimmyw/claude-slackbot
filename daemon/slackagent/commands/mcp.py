"""`|mcp` — what the agent can reach through the host's MCP proxy, and on whose behalf."""
from __future__ import annotations

import argparse
import datetime
import time

from ..mcpconfig import MODE_PER_USER, decide
from . import COMMAND_PREFIX, Context, SlackParser

NAME = "mcp"
ALIASES = ("tools",)
SUMMARY = "MCP servers, who may call what, and recent calls"

_DESCRIPTION = f"""\
Show and change what the agent can reach through the MCP proxy.

Credentials live on the host, in a file this command never prints and never edits. The
guest reaches upstreams only through the proxy, and only for the lifetime of a run, so
what you allow here is the whole of what a compromised VM could do.

  {COMMAND_PREFIX}mcp                          servers, credentials, policy, activity
  {COMMAND_PREFIX}mcp tools <server>           ask the upstream what it offers, live
  {COMMAND_PREFIX}mcp allow <server> <pattern> permit a tool (glob), from now on
  {COMMAND_PREFIX}mcp deny <server> <pattern>  refuse one; a deny always wins
  {COMMAND_PREFIX}mcp forget <id>              drop a runtime rule added above
  {COMMAND_PREFIX}mcp disable <server>         stop offering it at all
  {COMMAND_PREFIX}mcp enable <server>          offer it again
  {COMMAND_PREFIX}mcp calls                    the audit trail, newest first

`allow` and `deny` take effect on the next message, like `{COMMAND_PREFIX}auth`. Add
`--user <id>` to scope a rule to one person; without it the rule covers everyone. A
per-user allow list REPLACES the server's for that person, so it can narrow as well as
widen — and a deny beats every allow, wherever it came from.

Adding a whole server is a block in the host config file: it is re-read when it
changes, so a server added a minute ago is live on the next message.
"""

_ACTIONS = ("show", "tools", "allow", "deny", "forget", "disable", "enable", "calls")


def build_parser() -> argparse.ArgumentParser:
    parser = SlackParser(
        prog=f"{COMMAND_PREFIX}{NAME}",
        description=_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("action", nargs="?", default="show", choices=_ACTIONS)
    parser.add_argument("target", nargs="?", help="a server name, or a rule id")
    parser.add_argument("pattern", nargs="?", help="a tool name or glob")
    parser.add_argument(
        "--user", default="", help="scope an allow/deny to one Slack user id"
    )
    parser.add_argument(
        "--limit", type=int, default=15, help="how many calls to show"
    )
    return parser


async def run(ctx: Context, args: argparse.Namespace) -> None:
    proxy = ctx.mcp
    registry = getattr(proxy, "registry", None)
    if registry is None or (registry.path is None and not registry.servers()):
        await ctx.say(
            "No host-side MCP is configured, so the agent uses whatever MCP servers "
            "are set up inside the VM — including their credentials. Point "
            "`MCP_CONFIG` at a 0600 file to move them out here."
        )
        return

    if args.action == "show":
        await _show(ctx, registry)
    elif args.action == "tools":
        await _tools(ctx, proxy, registry, args)
    elif args.action in ("allow", "deny"):
        await _rule(ctx, registry, args)
    elif args.action == "forget":
        await _forget(ctx, args)
    elif args.action in ("enable", "disable"):
        await _toggle(ctx, registry, args)
    else:
        await _calls(ctx, args)


def _server_or_error(registry, name: str | None) -> object:
    from . import CommandError

    if not name:
        raise CommandError(
            f"which server? `{COMMAND_PREFIX}mcp` lists them."
        )
    server = registry.get(name)
    if server is None:
        known = ", ".join(sorted(registry.servers())) or "none"
        raise CommandError(f"no MCP server called `{name}`. Configured: {known}")
    return server


async def _show(ctx: Context, registry) -> None:  # noqa: ANN001
    servers = registry.servers()
    disabled = ctx.store.mcp_disabled()
    overrides = ctx.store.mcp_policy()
    since = int(time.time()) - 7 * 24 * 3600
    activity: dict[tuple[str, str], int] = {}
    for row in ctx.store.mcp_call_summary(since):
        activity[(row["server"], row["decision"])] = (
            activity.get((row["server"], row["decision"]), 0) + row["n"]
        )

    lines = [
        f"*MCP servers* — credentials live on the host, in `{registry.path}`. "
        "The VM holds none of them."
    ]
    if registry.error:
        lines.append(f":warning: {registry.error}")
    if not servers:
        lines.append("\nNothing configured.")

    for name, server in sorted(servers.items()):
        state = ":no_entry: disabled" if name in disabled else "enabled"
        who = (
            f"per-user ({len(server.per_user)} with credentials"
            + (", shared fallback on" if server.shared_fallback else "")
            + ")"
            if server.mode == MODE_PER_USER
            else "shared credential"
        )
        allowed = ", ".join(f"`{p}`" for p in server.allow) or "_nothing_"
        denied = ", ".join(f"`{p}`" for p in server.deny)
        rules = [r for r in overrides if r["server"] == name]
        counts = [
            f"{activity[(name, d)]} {d}"
            for d in ("allowed", "denied", "capped", "error")
            if (name, d) in activity
        ]
        lines.append(
            f"\n  *{name}* ({server.type}) — {state}, {who}"
            f"\n    allow: {allowed}"
            + (f"\n    deny: {denied}" if denied else "")
            + (
                "\n    runtime: "
                + ", ".join(
                    f"[{r['id']}] {r['effect']} `{r['pattern']}`"
                    + (f" for <@{r['slack_user']}>" if r["slack_user"] else "")
                    for r in rules
                )
                if rules
                else ""
            )
            + (f"\n    7 days: {', '.join(counts)}" if counts else "")
        )

        if server.mode == MODE_PER_USER:
            mine = (
                "you have a credential here"
                if ctx.user in server.per_user
                else "you have none, so this server is not offered to you"
            )
            lines.append(f"    {mine}")

    lines.append(
        f"\n`{COMMAND_PREFIX}mcp tools <server>` asks an upstream what it offers, "
        "marking what is allowed."
    )
    await ctx.say("\n".join(lines))


async def _tools(ctx: Context, proxy, registry, args) -> None:  # noqa: ANN001
    from . import CommandError

    server = _server_or_error(registry, args.target)
    user = args.user or ctx.user
    try:
        tools = await proxy.probe_tools(server, user)
    except Exception as exc:  # noqa: BLE001
        raise CommandError(
            f"could not ask `{server.name}` what it offers: {exc}"
        ) from exc

    if not tools:
        await ctx.say(f"`{server.name}` reported no tools at all.")
        return

    extra_allow = tuple(
        r["pattern"] for r in ctx.store.mcp_policy(server.name)
        if r["effect"] == "allow" and r["slack_user"] in ("", user)
    )
    extra_deny = tuple(
        r["pattern"] for r in ctx.store.mcp_policy(server.name)
        if r["effect"] == "deny" and r["slack_user"] in ("", user)
    )

    allowed: list[str] = []
    blocked: list[str] = []
    for tool in tools:
        name = tool.get("name") or "?"
        verdict = decide(
            server, user, name, extra_allow=extra_allow, extra_deny=extra_deny
        )
        (allowed if verdict.allowed else blocked).append(name)

    lines = [
        f"*{server.name}* offers {len(tools)} tool"
        f"{'s' if len(tools) != 1 else ''}, as <@{user}> would see them:"
    ]
    lines.append(
        "\n  :white_check_mark: allowed: "
        + (", ".join(f"`{n}`" for n in sorted(allowed)) or "_none_")
    )
    if blocked:
        lines.append(
            "  :no_entry: blocked (the agent is not even shown these): "
            + ", ".join(f"`{n}`" for n in sorted(blocked))
        )
        lines.append(
            f"\nAllow one with `{COMMAND_PREFIX}mcp allow {server.name} "
            f"{sorted(blocked)[0]}`."
        )
    await ctx.say("\n".join(lines))


async def _rule(ctx: Context, registry, args) -> None:  # noqa: ANN001
    from . import CommandError

    server = _server_or_error(registry, args.target)
    if not args.pattern:
        raise CommandError(
            f"which tool? e.g. `{COMMAND_PREFIX}mcp {args.action} {server.name} "
            "pulse_*`"
        )
    rule_id = ctx.store.add_mcp_policy(
        server.name, args.action, args.pattern, ctx.user, slack_user=args.user
    )
    scope = f" for <@{args.user}>" if args.user else " for everyone"
    note = (
        ""
        if args.action == "deny"
        else "\nA deny still wins over this, wherever it comes from."
    )
    await ctx.say(
        f":white_check_mark: `{args.pattern}` is now *{args.action}ed* on "
        f"`{server.name}`{scope}, from the next message. Rule id {rule_id} — "
        f"`{COMMAND_PREFIX}mcp forget {rule_id}` to drop it.{note}"
    )


async def _forget(ctx: Context, args) -> None:  # noqa: ANN001
    from . import CommandError

    try:
        rule_id = int(args.target or "")
    except ValueError as exc:
        raise CommandError(
            f"which rule? `{COMMAND_PREFIX}mcp` lists them with their ids."
        ) from exc
    if ctx.store.remove_mcp_policy(rule_id):
        await ctx.say(f"Dropped rule {rule_id}, from the next message.")
    else:
        await ctx.say(f"No runtime rule with id {rule_id}. Nothing changed.")


async def _toggle(ctx: Context, registry, args) -> None:  # noqa: ANN001
    server = _server_or_error(registry, args.target)
    enable = args.action == "enable"
    changed = ctx.store.set_mcp_enabled(server.name, enable, ctx.user)
    state = "offered again" if enable else "no longer offered"
    if not changed:
        await ctx.say(f"`{server.name}` was already {state}. Nothing changed.")
        return
    await ctx.say(
        f":white_check_mark: `{server.name}` is {state}, from the next message."
        + ("" if enable else " A run already in flight keeps what it started with.")
    )


async def _calls(ctx: Context, args) -> None:  # noqa: ANN001
    rows = ctx.store.recent_mcp_calls(max(1, min(args.limit, 50)))
    if not rows:
        await ctx.say("No MCP calls recorded yet.")
        return
    icons = {"allowed": ":white_check_mark:", "denied": ":no_entry:",
             "capped": ":scissors:", "error": ":warning:"}
    lines = [f"*Recent MCP calls* — {len(rows)} newest first:"]
    for row in rows:
        when = datetime.datetime.fromtimestamp(row["called_at"]).strftime("%m-%d %H:%M")
        detail = f" — {row['reason']}" if row["reason"] else ""
        lines.append(
            f"  {icons.get(row['decision'], '?')} `{when}` <@{row['slack_user']}> "
            f"{row['server']}.{row['tool']}{detail}"
        )
    await ctx.say("\n".join(lines))
