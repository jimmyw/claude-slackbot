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

    await ctx.say(
        f"VM `{ctx.config.vm_domain}`: {state}{f' at {ip}' if ip else ''}\n"
        f"SSH bridge: {reachable}\n"
        f"Policy: {policy} (`{COMMAND_PREFIX}auth` to change)\n"
        f"Standing grants: {len(grants)} (`{COMMAND_PREFIX}grants` to list)"
    )
