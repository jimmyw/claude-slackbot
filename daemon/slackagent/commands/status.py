"""`|status` — VM and bridge health, without spending a Claude turn."""
from __future__ import annotations

import argparse

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
    policy = ctx.store.get_setting("agent_policy", ctx.config.agent_policy)
    # Reported because a paused thread is silent by design, which is otherwise
    # indistinguishable from a broken one — and |status is where you look.
    here = ctx.store.is_thread_paused(ctx.channel, ctx.thread_ts)
    paused_elsewhere = len(ctx.store.list_paused()) - (1 if here else 0)

    await ctx.say(
        f"VM `{ctx.config.vm_domain}`: {state}{f' at {ip}' if ip else ''}\n"
        f"SSH bridge: {reachable}\n"
        f"Policy: {policy}"
        + (" — :warning: approvals disabled" if policy == "open" else "")
        + f" (`{COMMAND_PREFIX}auth` to change)\n"
        f"Standing grants: {len(grants)} (`{COMMAND_PREFIX}grants` to list)\n"
        + (
            ":large_orange_circle: This thread is *paused* — nothing here reaches "
            f"Claude (`{COMMAND_PREFIX}resume` to lift it)"
            if here
            else f"This thread: answering (`{COMMAND_PREFIX}pause` to mute it)"
        )
        + (
            f"\nPaused elsewhere: {paused_elsewhere} thread"
            f"{'s' if paused_elsewhere != 1 else ''}"
            if paused_elsewhere > 0
            else ""
        )
    )
