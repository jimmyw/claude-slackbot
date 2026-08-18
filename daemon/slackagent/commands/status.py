"""`|status` — VM and bridge health, without spending a Claude turn."""
from __future__ import annotations

import argparse
import datetime

from ..store import MODE_PAUSED, MODE_SILENT
from . import COMMAND_PREFIX, Context, SlackParser

NAME = "status"
ALIASES = ()
SUMMARY = "VM state, SSH bridge, grant count"


def build_parser() -> argparse.ArgumentParser:
    return SlackParser(
        prog=f"{COMMAND_PREFIX}{NAME}",
        description=(
            "Report whether the agent VM is running, whether the SSH bridge "
            "answers, and how many standing grants exist. Costs nothing: no "
            "Claude turn is started."
        ),
    )


async def run(ctx: Context, args: argparse.Namespace) -> None:
    state = await ctx.vm.state()
    ip = await ctx.vm.ip_address()
    probe = await ctx.bridge.probe()
    # agent-exec exits 64 on an empty job, which means SSH authenticated and the
    # forced command ran — a healthy path, not a failure.
    reachable = "reachable" if probe.exit_code in {0, 64} else "unreachable"
    grants = ctx.store.list_grants()
    registry = getattr(ctx.mcp, "registry", None)
    mcp_servers = sorted(registry.servers()) if registry is not None else []
    mcp_disabled = ctx.store.mcp_disabled() if mcp_servers else set()
    policy = ctx.store.get_setting("agent_policy", ctx.config.agent_policy)
    # Reported because a paused thread is silent by design, which is otherwise
    # indistinguishable from a broken one — and |status is where you look.
    here = ctx.store.thread_mode_entry(ctx.channel, ctx.thread_ts)
    elsewhere = [
        m for m in ctx.store.list_thread_modes()
        if (m.channel_id, m.thread_ts) != (ctx.channel, ctx.thread_ts)
    ]

    await ctx.say(
        f"VM `{ctx.config.vm_domain}`: {state}{f' at {ip}' if ip else ''}\n"
        f"SSH bridge: {reachable}\n"
        f"Policy: {policy}"
        + (" — :warning: approvals disabled" if policy == "open" else "")
        + f" (`{COMMAND_PREFIX}auth` to change)\n"
        f"Standing grants: {len(grants)} (`{COMMAND_PREFIX}grants` to list)\n"
        + (
            f"MCP: {len(mcp_servers) - len(mcp_disabled)} of {len(mcp_servers)} "
            f"server(s) offered, credentials on the host "
            f"(`{COMMAND_PREFIX}mcp`)\n"
            if mcp_servers
            else "MCP: none on the host, so the VM's own config is used "
                 f"(`{COMMAND_PREFIX}mcp`)\n"
        )
        + f"This thread: {_thread_line(here)}"
        + (f"\nElsewhere: {_elsewhere_line(elsewhere)}" if elsewhere else "")
        + (
            "\n_I may also stay quiet on a message that did not mention me, when I "
            "judge it was not for me. That is logged, not shown._"
            if here is None
            else ""
        )
    )


def _thread_line(entry) -> str:  # noqa: ANN001
    """One line naming which kind of quiet is in force.

    Three kinds now, not two: paused, mention-only, and the agent's own judgement on
    an unaddressed message. All three look identical in Slack — nothing happens — so
    this is the only place they can be told apart.
    """
    if entry is None:
        return (
            f"answering normally (`{COMMAND_PREFIX}silent` for mentions only, "
            f"`{COMMAND_PREFIX}pause` to mute)"
        )
    when = datetime.datetime.fromtimestamp(entry.set_at).strftime("%Y-%m-%d %H:%M")
    who = f" by <@{entry.set_by}>" if entry.set_by else ""
    dropped = (
        f", {entry.dropped} message{'s' if entry.dropped != 1 else ''} not forwarded"
        if entry.dropped else ""
    )
    label = (
        ":speech_balloon: *mention-only* — I answer when tagged"
        if entry.mode == MODE_SILENT
        else ":large_orange_circle: *paused* — nothing here reaches Claude"
    )
    return (
        f"{label} (set{who} at {when}{dropped}; "
        f"`{COMMAND_PREFIX}resume` to lift)"
    )


def _elsewhere_line(entries: list) -> str:
    silent = sum(1 for m in entries if m.mode == MODE_SILENT)
    paused = sum(1 for m in entries if m.mode == MODE_PAUSED)
    bits = []
    if silent:
        bits.append(f"{silent} mention-only")
    if paused:
        bits.append(f"{paused} paused")
    return ", ".join(bits) + " (a mode only covers the thread it was set in)"
