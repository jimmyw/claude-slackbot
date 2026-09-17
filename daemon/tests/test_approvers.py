"""The approver role: who may resolve an approval, and who may change that list.

The gate used to be one user-ID comparison, so widening it is exactly the kind of
change that can quietly widen it too far. What is pinned here:

  * a guest is still refused, and their click still leaves the request pending;
  * an approver can decide, and can press "Always allow" — which is a real
    widening, so the grant records THEM as its creator;
  * a person added while an approval is already waiting can answer that approval,
    because the decider set is read per click rather than per run;
  * removing an approver takes effect immediately;
  * `|approvers` is operator-only, and refuses to pretend it can remove an id that
    comes from .env.

Run:  .venv/bin/python -m tests.test_approvers
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from slackagent import roles
from slackagent.approvals import (
    ACTION_ALWAYS,
    ACTION_APPROVE,
    ACTION_DENY,
    ApprovalService,
)
from slackagent.commands import CommandError, CommandHelp, Context
from slackagent.commands import approvers as approvers_cmd
from slackagent.config import Config, ConfigError
from slackagent.store import Store
from tests.test_approvals import FakeRespond, FakeSlack, click, post_approve

# Shaped like real Slack ids — uppercase alphanumerics, no underscore — because
# that is what |approvers validates against, and an id it rejects would make the
# command tests pass for the wrong reason.
OPERATOR = "U0OPERATOR"
SEEDED = "U0IVO"          # from APPROVERS in .env
ADDED = "U0JONAS"         # added at runtime with |approvers
GUEST = "U0BOB"

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


def make_config(
    db_path: Path, port: int = 19301, *,
    approvers: frozenset[str] = frozenset(),
    allowed_users: frozenset[str] = frozenset(),
) -> Config:
    key = db_path.parent / "fake_key"
    key.write_text("not a real key")
    return Config(
        bot_token="xoxb-fake", app_token="xapp-fake",
        authorized_user=OPERATOR, allowed_users=allowed_users,
        vm_host="127.0.0.1", vm_user="agent", vm_ssh_key=key,
        vm_domain="agent-vm", vm_workdir="/home/agent/work",
        libvirt_uri="qemu:///system", forward_agent=False,
        agent_policy="permissive",
        approval_host="127.0.0.1", approval_port=port, approval_timeout_s=30,
        tunnel_port_low=9101, tunnel_port_high=9199,
        db_path=db_path, update_interval_s=0.0,
        approvers=approvers,
    )


# -- the rule, on its own ----------------------------------------------------


def test_rule() -> None:
    print("\n[1] the role model")
    check("nothing configured means the operator alone",
          roles.deciders(OPERATOR, (), ()) == {OPERATOR})
    check("the operator is an approver even if nobody says so",
          roles.may_approve(OPERATOR, OPERATOR, ()))
    check("a guest is not", not roles.may_approve(GUEST, OPERATOR, {SEEDED}))
    check("the seed and the runtime list are UNIONED, not overridden",
          roles.deciders(OPERATOR, {SEEDED}, {ADDED})
          == {OPERATOR, SEEDED, ADDED})
    check("the operator outranks a duplicate row",
          roles.role(OPERATOR, OPERATOR, {OPERATOR}) == roles.ROLE_OPERATOR)
    check("an approver is named as one",
          roles.role(SEEDED, OPERATOR, {SEEDED}) == roles.ROLE_APPROVER)
    check("everyone else is a guest",
          roles.role(GUEST, OPERATOR, {SEEDED}) == roles.ROLE_GUEST)
    # An empty id must never become a decider: `body["user"]["id"]` can be absent,
    # and requested_by is "" for a run with no known requester.
    check("the empty string is never a decider",
          "" not in roles.deciders("", {""}, {""}))
    check("and an empty clicker cannot approve",
          not roles.may_approve("", OPERATOR, {SEEDED}))


def test_config() -> None:
    print("\n[2] APPROVERS from the environment")
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        config = make_config(tmp / "s.sqlite3", approvers=frozenset({"U0A"}))
        config.validate()
        check("a valid list validates", config.approvers == {"U0A"})

        bad = make_config(tmp / "s.sqlite3", approvers=frozenset({"jonas"}))
        try:
            bad.validate()
            check("a name instead of an id is rejected", False)
        except ConfigError as exc:
            # Otherwise the failure is invisible: the id never matches, the person
            # clicks Approve, and the only trace is a log line calling them
            # unauthorized.
            check("a name instead of an id is rejected", "APPROVERS" in str(exc),
                  str(exc))


def test_store() -> None:
    print("\n[3] the runtime list in sqlite")
    with tempfile.TemporaryDirectory() as raw:
        store = Store(Path(raw) / "s.sqlite3")
        try:
            check("a fresh database has no approvers", store.approvers() == set())
            check("adding one reports the change",
                  store.add_approver(ADDED, OPERATOR) is True)
            check("it is in the list", store.approvers() == {ADDED})
            check("adding it twice reports no change",
                  store.add_approver(ADDED, OPERATOR) is False)
            row = store.approver_entries()[0]
            check("the row records who added them",
                  row["added_by"] == OPERATOR, dict(row))
            check("and when", int(row["added_at"]) > 0)
            check("removing reports the change",
                  store.remove_approver(ADDED) is True)
            check("removing again reports none",
                  store.remove_approver(ADDED) is False)
            check("the list is empty again", store.approvers() == set())
        finally:
            store.close()


# -- the gate ----------------------------------------------------------------


async def _ask(service: ApprovalService, slack: FakeSlack, port: int,
               token: str, command: str, use_id: str) -> tuple[asyncio.Task, str]:
    """Fire one hook request and return (task, approval_id) once it has posted."""
    before = len(slack.posted)
    task = asyncio.create_task(
        post_approve(
            port,
            {"run_token": token, "tool_name": "Bash",
             "tool_input": {"command": command}, "tool_use_id": use_id},
        )
    )
    for _ in range(50):
        await asyncio.sleep(0.02)
        if len(slack.posted) > before:
            break
    blocks = slack.posted[-1]["blocks"]
    actions = [b for b in blocks if b["type"] == "actions"][0]
    return task, actions["elements"][0]["value"]


async def test_gate(tmp: Path) -> None:
    print("\n[4] who can actually resolve an approval")
    port = 19311
    db = tmp / "gate.sqlite3"
    store = Store(db)
    slack = FakeSlack()
    config = make_config(db, port, approvers=frozenset({SEEDED}))
    service = ApprovalService(config, store, slack)
    await service.start()
    try:
        service.register_run("tok", "C1", "1.1", "sess", requested_by=GUEST)

        # A guest, first: the check has to refuse before it accepts anyone new.
        task, approval_id = await _ask(service, slack, port, "tok", "npm test", "t1")
        respond = FakeRespond()
        body, action = click(ACTION_APPROVE, approval_id, GUEST)
        await service.handle_button(body, action, respond)
        check("a guest's click is refused",
              store.approval(approval_id)["state"] == "pending")
        check("the refusal names the operator",
              OPERATOR in respond.messages[0]["text"], respond.messages[0]["text"])
        check("it mentions that others may decide too",
              "approvers" in respond.messages[0]["text"],
              respond.messages[0]["text"])
        check("and does NOT replace the original message",
              respond.messages[0].get("replace_original") is False)
        check("the request is still waiting", not task.done())

        # The .env seed can decide.
        body, action = click(ACTION_APPROVE, approval_id, SEEDED)
        await service.handle_button(body, action, FakeRespond())
        verdict = await asyncio.wait_for(task, timeout=5)
        check("an APPROVERS id can approve", verdict.get("approved") is True, verdict)
        check("the audit row names them as the decider",
              store.approval(approval_id)["resolved_by"] == SEEDED)
        check("and still names the guest who asked",
              store.approval(approval_id)["requested_by"] == GUEST)

        # Someone added WHILE an approval is waiting must be able to answer it:
        # that is the case that actually happens.
        task, approval_id = await _ask(service, slack, port, "tok", "npm run build", "t2")
        respond = FakeRespond()
        body, action = click(ACTION_APPROVE, approval_id, ADDED)
        await service.handle_button(body, action, respond)
        check("someone not yet on the list is refused",
              store.approval(approval_id)["state"] == "pending")
        store.add_approver(ADDED, OPERATOR)
        body, action = click(ACTION_APPROVE, approval_id, ADDED)
        await service.handle_button(body, action, FakeRespond())
        verdict = await asyncio.wait_for(task, timeout=5)
        check("adding them mid-wait lets them answer THAT approval",
              verdict.get("approved") is True, verdict)

        # An approver may deny, too — the role is a decision, not a rubber stamp.
        task, approval_id = await _ask(service, slack, port, "tok", "rm -rf /tmp/x", "t3")
        body, action = click(ACTION_DENY, approval_id, ADDED)
        await service.handle_button(body, action, FakeRespond())
        verdict = await asyncio.wait_for(task, timeout=5)
        check("an approver can deny", verdict.get("approved") is False, verdict)

        # Removal is immediate, the same way adding is.
        store.remove_approver(ADDED)
        task, approval_id = await _ask(service, slack, port, "tok", "ls /tmp", "t4")
        respond = FakeRespond()
        body, action = click(ACTION_APPROVE, approval_id, ADDED)
        await service.handle_button(body, action, respond)
        check("a removed approver is refused at once",
              store.approval(approval_id)["state"] == "pending")
        body, action = click(ACTION_DENY, approval_id, OPERATOR)
        await service.handle_button(body, action, FakeRespond())
        await asyncio.wait_for(task, timeout=5)
    finally:
        await service.stop()
        store.close()


async def test_always_allow(tmp: Path) -> None:
    print("\n[5] an approver pressing 'Always allow'")
    port = 19312
    db = tmp / "always.sqlite3"
    store = Store(db)
    slack = FakeSlack()
    config = make_config(db, port, approvers=frozenset({SEEDED}))
    service = ApprovalService(config, store, slack)
    await service.start()
    try:
        service.register_run("tok", "C1", "2.2", "sess")
        task, _ = await _ask(service, slack, port, "tok", "ls -la /home/agent/work", "t1")
        buttons = [
            b for b in slack.posted[-1]["blocks"] if b["type"] == "actions"
        ][0]["elements"]
        always = next(b for b in buttons if "Always" in b["text"]["text"])

        body, action = click(ACTION_ALWAYS, always["value"], SEEDED)
        await service.handle_button(body, action, FakeRespond())
        verdict = await asyncio.wait_for(task, timeout=5)
        check("the call is approved", verdict.get("approved") is True, verdict)
        grants = store.list_grants()
        check("a standing grant was created", len(grants) == 1, grants)
        # The whole point of recording it: this grant now answers for everyone,
        # and it was not the operator who widened it.
        check("and it records the APPROVER as its creator",
              grants and grants[0].created_by == SEEDED,
              grants[0].created_by if grants else None)

        # A guest pressing the same button creates nothing.
        service.register_run("tok2", "C1", "2.2", "sess")
        task, approval_id = await _ask(service, slack, port, "tok2", "cat /etc/hosts", "t2")
        buttons = [
            b for b in slack.posted[-1]["blocks"] if b["type"] == "actions"
        ][0]["elements"]
        always = next(b for b in buttons if "Always" in b["text"]["text"])
        body, action = click(ACTION_ALWAYS, always["value"], GUEST)
        await service.handle_button(body, action, FakeRespond())
        check("a guest's 'Always allow' creates no grant",
              len(store.list_grants()) == 1, store.list_grants())
        body, action = click(ACTION_DENY, approval_id, OPERATOR)
        await service.handle_button(body, action, FakeRespond())
        await asyncio.wait_for(task, timeout=5)
    finally:
        await service.stop()
        store.close()


# -- the command -------------------------------------------------------------


class Said:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def __call__(self, text: str) -> None:
        self.messages.append(text)

    @property
    def last(self) -> str:
        return self.messages[-1] if self.messages else ""


async def run_cmd(config: Config, store: Store, said: Said, argv: str,
                  *, operator: bool = True, user: str = OPERATOR) -> None:
    args = approvers_cmd.build_parser().parse_args(argv.split())
    ctx = Context(
        channel="C1", thread_ts="1.1", message_ts="1.2", user=user,
        is_operator=operator, config=config, store=store, vm=None, bridge=None,
        approvals=None, say=said,
    )
    await approvers_cmd.run(ctx, args)


async def test_command(tmp: Path) -> None:
    print("\n[6] |approvers")
    db = tmp / "cmd.sqlite3"
    store = Store(db)
    config = make_config(db, approvers=frozenset({SEEDED}),
                         allowed_users=frozenset({OPERATOR}))
    said = Said()
    try:
        # Operator-only, checked in the command itself and not only by the caller.
        try:
            await run_cmd(config, store, said, "add <@U0NEW>",
                          operator=False, user=GUEST)
            check("a guest cannot change the list", False)
        except CommandError as exc:
            check("a guest cannot change the list", OPERATOR in str(exc), str(exc))
        check("and nothing was added", store.approvers() == set())

        # The mention form is what someone actually types.
        await run_cmd(config, store, said, "add <@U0NEW>")
        check("a tagged user is added", store.approvers() == {"U0NEW"},
              store.approvers())
        check("the reply says what they can and cannot do",
              "Always allow" in said.last and "cannot" in said.last, said.last)
        await run_cmd(config, store, said, "remove U0NEW")
        check("a bare id removes them", store.approvers() == set())
        await run_cmd(config, store, said, f"add <@{ADDED}|jonas>")
        check("the older <@id|handle> form parses too",
              store.approvers() == {ADDED}, store.approvers())

        # A malformed id must be an argparse error, not a row nobody matches.
        for bad in ("add jonas", "add @jonas", "add <@lowercase>"):
            try:
                await run_cmd(config, store, said, bad)
                check(f"{bad!r} is refused", False)
            except CommandError:
                check(f"{bad!r} is refused", True)
        check("nothing malformed was stored", store.approvers() == {ADDED},
              store.approvers())

        # The .env seed: listable, not removable.
        try:
            await run_cmd(config, store, said, f"remove <@{SEEDED}>")
            check("removing an .env seed is refused", False)
        except CommandError as exc:
            check("removing an .env seed is refused", ".env" in str(exc), str(exc))
        await run_cmd(config, store, said, f"add <@{SEEDED}>")
        check("adding one already in .env changes nothing",
              store.approvers() == {ADDED}, store.approvers())
        check("and says why", "daemon/.env" in said.last, said.last)

        # The operator is a level, not a row.
        try:
            await run_cmd(config, store, said, f"remove <@{OPERATOR}>")
            check("the operator cannot be removed", False)
        except CommandError as exc:
            check("the operator cannot be removed",
                  "AUTHORIZED_USER_ID" in str(exc), str(exc))
        await run_cmd(config, store, said, f"add <@{OPERATOR}>")
        check("adding the operator is a no-op",
              store.approvers() == {ADDED}, store.approvers())

        # Removing someone who was never on the list must not claim a change.
        await run_cmd(config, store, said, "remove <@U0NOBODY>")
        check("removing a non-approver says nothing changed",
              "Nothing changed" in said.last, said.last)

        # The listing.
        await run_cmd(config, store, said, "")
        listing = said.last
        for who, label in ((OPERATOR, "operator"), (SEEDED, "APPROVERS"),
                           (ADDED, "added")):
            check(f"the listing names {who} as {label}",
                  f"<@{who}>" in listing and label in listing, listing)
        check("it counts everyone who can decide", "3 " in listing, listing)
        # ALLOWED_USERS is set to the operator alone here, so both approvers can
        # decide and neither can ask for anything. Invisible from Slack otherwise.
        check("it warns about approvers who cannot talk to the bot",
              "ALLOWED_USERS" in listing, listing)

        # -h has to arrive as CommandHelp, not CommandError: it is usage, not a
        # failure, and the dispatcher renders the two differently.
        try:
            await run_cmd(config, store, said, "-h")
            check("-h reports the help", False)
        except CommandHelp as help_text:
            check("the help explains the limits of the role",
                  "Always allow" in str(help_text)
                  and "ALLOWED_USERS" in str(help_text),
                  str(help_text)[:400])
    finally:
        store.close()


async def main() -> int:
    test_rule()
    test_config()
    test_store()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        await test_gate(tmp)
        await test_always_allow(tmp)
        await test_command(tmp)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
