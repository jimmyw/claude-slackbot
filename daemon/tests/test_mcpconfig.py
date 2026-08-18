"""MCP server definitions, credential selection, per-user policy, host-side tables.

Pure resolution rules, so this is where the security model is actually pinned down:
who gets which credential, which tools they may call, and what happens to a config
file that is malformed or readable by other people on the host.

Run:  .venv/bin/python -m tests.test_mcpconfig
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from slackagent.store import Store
from slackagent.mcpconfig import (
    DEFAULT_MAX_CALLS_PER_RUN,
    McpConfigError,
    Registry,
    available_for,
    decide,
    parse,
    parse_server,
)

OPERATOR = "U_JIMMY"
GUEST = "U_BOB"

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


def raises(label: str, fn, needle: str = "") -> None:
    try:
        fn()
    except McpConfigError as exc:
        check(label, needle in str(exc), f"message was {exc}")
    except Exception as exc:  # noqa: BLE001
        check(label, False, f"raised {type(exc).__name__}: {exc}")
    else:
        check(label, False, "did not raise")


DOC = {
    "servers": {
        "syslog": {
            "type": "http",
            "url": "https://syslog.example/mcp",
            "credential": {"mode": "shared", "headers": {"Cookie": "s3cret"}},
            "tools": {"allow": ["query_logs", "list_hosts", "get_*"]},
        },
        "varys": {
            "type": "stdio",
            "command": "/opt/varys/bin/python3",
            "args": ["/opt/varys/varys_mcp.py"],
            "credential": {
                "mode": "per_user",
                "users": {OPERATOR: {"env": {"VARYS_TOKEN": "t0ken"}}},
            },
            "tools": {
                "allow": ["pulse_command", "pulse_stream"],
                "deny": ["pulse_reboot"],
                "users": {OPERATOR: {"allow": ["pulse_*"]}},
            },
        },
    }
}


def test_parsing() -> None:
    print("\n[1] parsing and validation")
    servers = parse(DOC)
    check("both servers parse", sorted(servers) == ["syslog", "varys"], sorted(servers))
    check("http keeps its url", servers["syslog"].url.endswith("/mcp"))
    check("stdio keeps command and args",
          servers["varys"].args == ("/opt/varys/varys_mcp.py",))
    check("caps have defaults",
          servers["syslog"].max_calls_per_run == DEFAULT_MAX_CALLS_PER_RUN)

    # A typo must not read as "no policy" — that silently denies everything, or
    # silently presents no credential, and looks like a broken upstream.
    raises("an unknown server key is refused",
           lambda: parse_server("x", {"type": "http", "url": "u", "toolz": {}}),
           "unknown key")
    raises("an unknown tools key is refused",
           lambda: parse_server("x", {"type": "http", "url": "u",
                                      "tools": {"alow": ["a"]}}),
           "unknown key")
    raises("an unknown transport is refused",
           lambda: parse_server("x", {"type": "grpc"}), "expected one of")
    raises("http without a url is refused",
           lambda: parse_server("x", {"type": "http"}), "needs a url")
    raises("stdio without a command is refused",
           lambda: parse_server("x", {"type": "stdio"}), "needs a command")
    raises("allow must be a list",
           lambda: parse_server("x", {"type": "http", "url": "u",
                                      "tools": {"allow": "everything"}}),
           "list of strings")
    raises("per_user with nobody in it is refused, since nobody could use it",
           lambda: parse_server("x", {"type": "http", "url": "u",
                                      "credential": {"mode": "per_user"}}),
           "lists no users")


def test_credentials() -> None:
    print("\n[2] which credential a caller gets")
    servers = parse(DOC)
    syslog, varys = servers["syslog"], servers["varys"]

    check("a shared server gives everyone the same credential",
          syslog.credential_for(GUEST) is not None
          and syslog.credential_for(GUEST).headers.get("Cookie") == "s3cret")
    check("a per_user server gives the operator theirs",
          (varys.credential_for(OPERATOR) or None) is not None
          and varys.credential_for(OPERATOR).env.get("VARYS_TOKEN") == "t0ken")
    check("and gives a guest with no entry NOTHING — never the shared one",
          varys.credential_for(GUEST) is None)
    check("the credential remembers whose it is, for keying rotated oauth grants",
          varys.credential_for(OPERATOR).owner == OPERATOR)

    with_fallback = parse_server("varys", {
        **DOC["servers"]["varys"],
        "credential": {"mode": "per_user", "shared_fallback": True,
                       "headers": {"X": "shared"},
                       "users": {OPERATOR: {"env": {"VARYS_TOKEN": "t"}}}},
    })
    check("shared_fallback is opt-in, per server, and then a guest does get it",
          (with_fallback.credential_for(GUEST) or None) is not None
          and with_fallback.credential_for(GUEST).headers == {"X": "shared"})


def test_policy() -> None:
    print("\n[3] which tools a caller may call")
    servers = parse(DOC)
    syslog, varys = servers["syslog"], servers["varys"]

    check("an allowed tool is allowed", decide(syslog, GUEST, "query_logs").allowed)
    check("a glob works", decide(syslog, GUEST, "get_stats").allowed)
    check("anything unlisted is denied by default",
          not decide(syslog, GUEST, "delete_everything").allowed)
    check("and says why", "not on the allowlist"
          in decide(syslog, GUEST, "delete_everything").reason)

    check("a server-wide deny beats the server-wide allow",
          not decide(varys, GUEST, "pulse_reboot").allowed)
    check("a per-user allow REPLACES the server allow, so it can narrow as well as "
          "widen", decide(varys, OPERATOR, "pulse_lookup_qr").allowed
          and not decide(varys, GUEST, "pulse_lookup_qr").allowed)
    check("but a deny still wins over a per-user allow",
          not decide(varys, OPERATOR, "pulse_reboot").allowed,
          decide(varys, OPERATOR, "pulse_reboot").reason)

    print("\n[3b] runtime patterns from |mcp")
    check("an extra allow widens one caller",
          decide(syslog, GUEST, "tail_file", extra_allow=("tail_*",)).allowed)
    # This is the bug the |mcp test caught: a runtime allow used to land in the
    # per-user slot, which REPLACES the file's list, so allowing one tool silently
    # revoked every other tool on the server.
    check("and does not revoke what the file already allowed",
          decide(syslog, GUEST, "query_logs", extra_allow=("tail_*",)).allowed)
    check("nor does it override a per-user narrowing in the file",
          decide(varys, OPERATOR, "pulse_lookup_qr",
                 extra_allow=("something_else",)).allowed)
    check("an extra deny wins over everything",
          not decide(syslog, GUEST, "query_logs", extra_deny=("query_*",)).allowed)


def test_registry() -> None:
    print("\n[4] the file: hot reload, bad JSON, bad permissions")
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "mcp.json"
        path.write_text(json.dumps(DOC))
        os.chmod(path, 0o600)

        registry = Registry(path)
        check("it loads", sorted(registry.servers()) == ["syslog", "varys"])

        # Hot reload: a new server appears without a restart.
        doc2 = json.loads(json.dumps(DOC))
        doc2["servers"]["esp-crash"] = {
            "type": "http", "url": "https://esp.example/mcp",
            "credential": {"mode": "shared",
                           "oauth": {"token_url": "https://esp.example/token",
                                     "client_id": "cid", "refresh_token": "r"}},
            "tools": {"allow": ["list_*"]},
        }
        path.write_text(json.dumps(doc2))
        os.utime(path, (0, 0))  # force a different mtime
        check("a newly added server appears with no restart",
              "esp-crash" in registry.servers(), sorted(registry.servers()))
        check("its oauth block is kept",
              (registry.get("esp-crash").shared.oauth or {}).get("client_id") == "cid")

        # A broken file keeps the last good copy: nothing new can be granted by a
        # parse failure, and losing every server to a stray comma is worse.
        path.write_text("{not json")
        os.utime(path, (1, 1))
        check("a broken file keeps the previous servers",
              sorted(registry.servers()) == ["esp-crash", "syslog", "varys"],
              sorted(registry.servers()))
        check("and says why, for |mcp to show", "Expecting" in registry.error
              or "json" in registry.error.lower(), registry.error)

        # It holds tokens, so a readable-by-others file is refused outright.
        path.write_text(json.dumps(DOC))
        os.chmod(path, 0o644)
        os.utime(path, (2, 2))
        check("a group/other-readable config is refused, not warned about",
              registry.servers() == {}, sorted(registry.servers()))
        check("with an actionable reason", "chmod 600" in registry.error,
              registry.error)

        os.chmod(path, 0o600)
        os.utime(path, (3, 3))
        check("fixing the mode brings it back",
              sorted(registry.servers()) == ["syslog", "varys"])

        print("\n[5] what a caller is offered")
        offered = available_for(registry, OPERATOR, disabled=set())
        check("the operator sees both", sorted(offered) == ["syslog", "varys"])
        offered = available_for(registry, GUEST, disabled=set())
        check("a guest without a varys credential is not offered varys at all — "
              "better than tools whose every call fails",
              sorted(offered) == ["syslog"], sorted(offered))
        offered = available_for(registry, OPERATOR, disabled={"syslog"})
        check("a disabled server is not offered", sorted(offered) == ["varys"])

    print("\n[6] no config at all")
    empty = Registry(None)
    check("no path means no servers, and no crash", empty.servers() == {})
    missing = Registry(Path("/nonexistent/mcp.json"))
    check("a missing file means no servers", missing.servers() == {})
    check("and says so", "does not exist" in missing.error, missing.error)


def test_store() -> None:
    print("\n[7] the host-side tables")
    with tempfile.TemporaryDirectory() as raw:
        store = Store(Path(raw) / "s.sqlite3")

        # The audit trail records every ATTEMPT, because a denied call is the more
        # interesting row: it says what the agent wanted.
        for tool, decision in [("query_logs", "allowed"), ("pulse_reboot", "denied")]:
            store.record_mcp_call(
                slack_user=GUEST if decision == "denied" else OPERATOR,
                channel_id="C1", thread_ts="1.1", session_id="s1",
                server="varys", tool=tool, decision=decision,
                reason="test", args_digest="d1", result_bytes=10, duration_ms=5,
            )
        rows = store.recent_mcp_calls()
        check("both attempts are recorded", len(rows) == 2, len(rows))
        check("newest first", rows[0]["tool"] == "pulse_reboot", rows[0]["tool"])
        check("the audit names the person, which the guest could not have faked",
              rows[0]["slack_user"] == GUEST, rows[0]["slack_user"])
        summary = {(r["tool"], r["decision"]): r["n"] for r in store.mcp_call_summary(0)}
        check("the summary groups by tool and decision",
              summary.get(("pulse_reboot", "denied")) == 1, summary)

        bad = False
        try:
            store.record_mcp_call(
                slack_user="U1", channel_id="C", thread_ts="1", session_id="s",
                server="x", tool="y", decision="probably",
            )
        except ValueError:
            bad = True
        check("an unknown decision raises rather than being stored", bad)

        print("\n[7b] oauth grants, keyed per person")
        store.save_mcp_token("esp-crash", "", access_token="a1",
                             refresh_token="r1", expires_at=100)
        store.save_mcp_token("esp-crash", OPERATOR, access_token="a2",
                             refresh_token="r2", expires_at=200)
        check("the shared grant and a personal one coexist",
              store.mcp_token("esp-crash", "")["access_token"] == "a1"
              and store.mcp_token("esp-crash", OPERATOR)["access_token"] == "a2")
        # Refresh tokens rotate; persisting the new one is what stops the first
        # refresh silently bricking the credential.
        store.save_mcp_token("esp-crash", OPERATOR, access_token="a3",
                             refresh_token="r3", expires_at=300)
        row = store.mcp_token("esp-crash", OPERATOR)
        check("a rotated refresh token replaces the old one",
              row["refresh_token"] == "r3" and row["expires_at"] == 300, dict(row))
        check("an unknown grant is None", store.mcp_token("nope") is None)

        print("\n[7c] runtime policy and disabling, from |mcp")
        first = store.add_mcp_policy("varys", "allow", "pulse_*", OPERATOR,
                                     slack_user=OPERATOR)
        again = store.add_mcp_policy("varys", "allow", "pulse_*", OPERATOR,
                                     slack_user=OPERATOR)
        check("adding the same pattern twice is idempotent", first == again)
        check("it is scoped to a server and a user",
              [(r["server"], r["slack_user"], r["effect"], r["pattern"])
               for r in store.mcp_policy("varys")]
              == [("varys", OPERATOR, "allow", "pulse_*")])
        check("removing it works", store.remove_mcp_policy(first)
              and store.mcp_policy("varys") == [])
        bad = False
        try:
            store.add_mcp_policy("varys", "maybe", "p", OPERATOR)
        except ValueError:
            bad = True
        check("an unknown effect raises", bad)

        check("a server starts enabled, with no row needed",
              store.mcp_disabled() == set())
        check("disabling is recorded", store.set_mcp_enabled("varys", False, OPERATOR)
              and store.mcp_disabled() == {"varys"})
        check("disabling twice changes nothing",
              not store.set_mcp_enabled("varys", False, OPERATOR))
        check("enabling deletes the row", store.set_mcp_enabled("varys", True)
              and store.mcp_disabled() == set())
        store.close()


def main() -> int:
    test_parsing()
    test_credentials()
    test_policy()
    test_registry()
    test_store()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
