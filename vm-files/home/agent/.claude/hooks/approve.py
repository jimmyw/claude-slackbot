#!/usr/bin/env python3
"""PreToolUse approval gate.

Runs inside the VM. Holds no Slack credential: it POSTs the pending tool call to
the daemon over the SSH reverse tunnel and blocks on the daemon's verdict, which
the daemon only returns after Jimmy clicks Approve or Deny in Slack.

The contract (verified against Claude Code 2.1.231):

  stdin   {"session_id", "transcript_path", "cwd", "prompt_id",
           "permission_mode", "effort", "hook_event_name",
           "tool_name", "tool_input", "tool_use_id"}

  stdout  {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                  "permissionDecision": "allow" | "deny",
                                  "permissionDecisionReason": "..."}}

Every failure path denies. A hook that errors out also blocks the tool (Claude
Code surfaces the failure as an error tool_result), so the gate is fail-closed
twice over — but we still emit an explicit deny so the model gets a readable
reason instead of a stack trace.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# Tools that never need a human. Read-only inspection of the working tree, plus
# the agent's own todo bookkeeping.
#
# Bash is deliberately absent: it is the universal escape hatch. A gate that
# blocks Write but allows Bash blocks nothing, because `bash -c 'echo > file'`
# does the same job. (Observed in testing: denied a Write, the agent immediately
# tried Bash alternatives.)
AUTO_ALLOW = frozenset(
    {
        "Read",
        "Grep",
        "Glob",
        "TodoWrite",
        "NotebookRead",
    }
)

# Must be comfortably below the hook `timeout` in settings.json, so that we
# return a real deny rather than being killed mid-wait by the harness.
REQUEST_TIMEOUT_S = 700


def decide(decision: str, reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    sys.stdout.flush()
    sys.exit(0)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        decide("deny", f"approval gate could not parse the hook payload: {exc}")

    tool_name = payload.get("tool_name") or "<unknown>"

    if tool_name in AUTO_ALLOW:
        decide("allow", f"{tool_name} is on the read-only allowlist")

    url = os.environ.get("AGENT_APPROVAL_URL")
    token = os.environ.get("AGENT_RUN_TOKEN")
    if not url or not token:
        decide(
            "deny",
            "approval gate is not wired up (AGENT_APPROVAL_URL / "
            "AGENT_RUN_TOKEN unset), so no human can approve this call",
        )

    body = json.dumps(
        {
            "run_token": token,
            "tool_name": tool_name,
            "tool_input": payload.get("tool_input"),
            "tool_use_id": payload.get("tool_use_id"),
            "session_id": payload.get("session_id"),
            "cwd": payload.get("cwd"),
        }
    ).encode()

    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            verdict = json.load(response)
    except urllib.error.HTTPError as exc:
        decide("deny", f"approval service rejected the request (HTTP {exc.code})")
    except urllib.error.URLError as exc:
        decide("deny", f"could not reach the approval service: {exc.reason}")
    except TimeoutError:
        decide("deny", "no answer from Slack before the approval window closed")
    except (json.JSONDecodeError, ValueError):
        decide("deny", "approval service returned a malformed verdict")

    approved = verdict.get("approved") is True
    reason = verdict.get("reason") or ("approved in Slack" if approved else "denied in Slack")
    decide("allow" if approved else "deny", reason)


if __name__ == "__main__":
    main()
