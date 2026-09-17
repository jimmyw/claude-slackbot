"""Who may do what.

Three levels, not two:

  operator   AUTHORIZED_USER_ID. Decides tool calls AND runs every `|` command —
             `|auth`, `|revoke`, `|approvers` — and is the only one who gets DMs.
             Exactly one person, named in .env, never removable from Slack.
  approver   May press Approve, Deny and "Always allow" on a tool call, and
             nothing else. Seeded by APPROVERS in .env and extended at runtime
             with `|approvers`.
  guest      Anyone else who can talk to the bot: may ask, may not decide.

Two properties this module exists to keep true:

  1. The operator is always an approver. The role is a level, not a list to keep
     in step with another one — so a database with no `approvers` rows and an
     empty APPROVERS behaves exactly as the daemon did before this existed.

  2. The .env seed and the runtime list are UNIONED, never overridden. The same
     rule `|mcp` learned the hard way: a runtime list that replaced the
     configured one silently revoked everything it did not repeat. Here it would
     mean adding one approver in Slack quietly removing three from .env. The
     cost is that a seeded id cannot be dropped from Slack, which `|approvers`
     says out loud rather than failing silently.

Nothing here reads a Slack profile: ids only, like everywhere else in the daemon.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

ROLE_OPERATOR = "operator"
ROLE_APPROVER = "approver"
ROLE_GUEST = "guest"


def deciders(operator: str, seeded: Iterable[str], added: Iterable[str]) -> frozenset[str]:
    """Everyone who may resolve an approval. Pure, so the rule is testable alone."""
    return frozenset({operator, *seeded, *added} - {""})


def load(config: Any, store: Any) -> frozenset[str]:
    """`deciders` for the live daemon. Touches sqlite, so call it in a thread."""
    return deciders(config.authorized_user, config.approvers, store.approvers())


def role(user: str, operator: str, approvers: Iterable[str]) -> str:
    """Which level a user is at. Used for display; the gate uses `deciders`."""
    if user and user == operator:
        return ROLE_OPERATOR
    if user and user in set(approvers):
        return ROLE_APPROVER
    return ROLE_GUEST


def may_approve(user: str, operator: str, approvers: Iterable[str]) -> bool:
    return role(user, operator, approvers) in (ROLE_OPERATOR, ROLE_APPROVER)
