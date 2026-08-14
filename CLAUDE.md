# Project: Slack-controlled Claude Code daemon

## Goal
A lasting support bot where the real work is done by a Claude Code session
running headless in a VM, controlled entirely through Slack. The agent
should document what it learns locally and carry that knowledge across
sessions.

## Architecture decisions (already made — don't relitigate these)

- **Execution engine:** Claude Code CLI, invoked headlessly via
  `claude -p "<prompt>" --output-format stream-json --resume <session_id>`.
  Not Hermes Agent, not OpenHands, not Letta as the primary runtime.
- **Isolation:** a real VM (libvirt/KVM) on the home server `terra` — not
  just a container. Outbound-only networking; no inbound, no LAN access.
- **Persistence:** the VM is long-lived, not spun up per session, so memory
  can accumulate. Periodic ZFS snapshots for rollback.
- **Daemon location:** the Slack-facing daemon runs OUTSIDE the VM (on
  terra), not inside it. It holds the Slack bot token and does the approval
  check. It bridges to the VM over SSH to invoke the Claude Code CLI.
- **Session mapping:** sqlite table `slack_thread_ts -> claude_session_id`.
  New thread = new session (no `--resume`). Reply in existing thread =
  `--resume <session_id>`.
- **Approval gate:** a PreToolUse hook posts an Approve/Deny message with
  buttons to Slack and blocks the CLI call until resolved. The button handler
  MUST check `body["user"]["id"] == AUTHORIZED_USER` (Jimmy's Slack user ID) —
  channel membership is not an access control, only the explicit user-ID
  check is. Timeout = deny by default.
- **Memory:** a simple `memory/MEMORY.md` index plus one file per memory,
  inside the VM. Claude Code is instructed via the guest `CLAUDE.md` to read
  relevant memory at session start and append/update at the end. Only
  graduate to a local vector store if the markdown approach becomes
  unwieldy — don't build that up front.
- **Lifecycle:** `virsh autostart` handles the VM at boot; a `systemd --user`
  unit manages the daemon's restart-on-crash.

## Resolved (was "Open / not yet decided")

- **Repo access — the VM clones its own repos.** No virtio-fs mount. There
  was no repo on terra to mount, and letting the VM own its clones over its
  own outbound network removes host-filesystem exposure entirely. `virtiofsd`
  is already installed as a transitive dep of `qemu-base` if this ever needs
  revisiting.
- **Daemon language — Python 3 + `slack_bolt` in Socket Mode.** Socket Mode is
  what makes the no-inbound constraint actually hold: no ports, no reverse
  proxy, works behind NAT.
- **Daemon packaging — a `systemd --user` unit in a venv, not a container.**
  It needs the libvirt socket and an SSH key; containerising means mounting
  both plus `--network=host` for no isolation gain, since it holds the Slack
  token either way. The boundary that matters is the VM.
- **SSH/RPC bridge — SSH out, reverse tunnel back.** One `ssh` per Slack
  message, with the VM's `authorized_keys` pinning the key to
  `/usr/local/bin/agent-exec` (a forced command — the key cannot get a
  shell). The same invocation carries `-R <port>:127.0.0.1:9100`, giving the
  in-VM approval hook a loopback route to the daemon's approval listener.
  The tunnel's lifetime is exactly the run's lifetime, which is exactly when
  the hook can fire — so the VM never holds a Slack token or talks to Slack.
  Each concurrent run gets its own guest-side port from a pool, plus a
  per-run token that identifies and authenticates it.
- **Idle-suspend policy — none.** The VM stays running: a 4 GB guest on a
  62 GB host does not justify start-on-demand latency. The daemon starts the
  domain if a message arrives while it is down. `virsh managedsave` after an
  idle period is documented as a later option, not implemented.
- **CLI auth in the VM — `claude setup-token`**, a long-lived OAuth token off
  the existing subscription. Egress must therefore permit
  `console.anthropic.com` for refresh, not just `api.anthropic.com`.
  Swapping to `ANTHROPIC_API_KEY` is a one-env-var change.

## Still open

- **Replace `jimmyw`'s key with a read-only machine user.** Forwarding is
  unconstrained, so the account is the entire security model; right now the guest
  can push to anything Jimmy can write during a run.
- Whether the agent should ever push. The guest CLAUDE.md tells it not to try,
  but with a personal key forwarded it currently could. Its git identity is set
  (`agent-vm (Claude Code) <jimmy@tibber.com>`) so local commits are clean.
- **Tailscale in the guest voids the egress guarantee.** As of 2026-08-13 the VM
  is on the tailnet as `agent-vm` (100.76.114.102) and can reach `bigjimmy` via
  DERP. nftables cannot see inside WireGuard, so `20-nftables-egress.sh` is no
  longer the control for tailnet destinations — tailnet ACLs are. terra itself is
  not a peer on that tailnet, so LAN-deny still holds for terra and 192.168.x.
  `verify-egress.sh` now FAILS loudly when it finds tailscaled running rather
  than reporting a green it cannot justify. Decide whether to restrict `agent-vm`
  with tailnet ACLs or to accept the tailnet as the boundary.
- Whether to tighten egress from the current LAN-deny baseline to a
  CONNECT-proxy hostname allowlist (see README, "Hardening").
- Whether to add thread-scoped, time-boxed grants alongside the persistent
  prefix ones. Prefix grants are built (see below); a whole-tool grant for Bash
  was deliberately not, because for arbitrary code it amounts to suspending the
  gate for the duration.

## Operational gaps found the hard way

- **There is no out-of-band way into the guest.** Both accounts have
  `lock_passwd: true`, so `virsh console` cannot log in. When sshd stopped
  serving (see below) the VM was pingable, idle, and completely unreachable.
  Nothing has been done about this yet; a console password for `admin` or a
  serial getty would be the fix.
- **Rapid ssh retries wedge sshd.** Each abandoned connection holds an
  unauthenticated slot for `LoginGraceTime` (120s default) and `MaxStartups` is
  `10:30:100`, so a burst of timed-out attempts makes *every* new connection
  accept the TCP handshake and then send no banner. Diagnosing by retrying makes
  it strictly worse. If TCP connects but no banner arrives: stop, wait two
  minutes, then try once.
- **`add-repo.sh` hung once at ssh session establishment** and could not be
  reproduced afterwards: the same clone via the same heredoc form completed in
  2.3s, and the guest was at 0% CPU with 3.7GB free during the hang. The failure
  was in getting an ssh session, not in git. Root cause unconfirmed; the retry
  storm above then sustained it for several minutes.

## Database migrations

- **`CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists**, so
  `SCHEMA` is only true of *fresh* databases. Every column added after a release
  needs an explicit migration in `Store._migrate`, or the first query touching it
  fails at runtime — which happened: `grants.match_type` broke `|status`, and
  `approvals.requested_by` would have crashed the next approval and with it the
  gate. Neither showed up in tests, because tests always start from an empty file.
- Adding a column is `ALTER TABLE ADD COLUMN`. **Changing a constraint is not**:
  sqlite cannot alter one in place, so `grants` is rebuilt (create, copy, drop,
  rename) because its UNIQUE gained `match_type`. Without the rebuild, adding an
  `exact` grant beside an existing `prefix` one for the same pattern raises
  IntegrityError.
- **Migrating data can be silently wrong even when the schema is right.** The first
  attempt labelled every existing grant `prefix`, including six whose pattern was
  `*` — the wildcard marker from before `match_type` existed. They became grants
  matching the literal string `*`, i.e. nothing, so the operator's grants would
  have quietly started asking again. The migration now maps `pattern = '*'` to
  `match_type = 'any'`.
- `test_grants.py` builds a database with the old schema and migrates it, which is
  the only way this class of bug is visible before deployment.

## Local commands (`|`-prefixed, never forwarded)

One module per command in `slackagent/commands/`, discovered with
`pkgutil.iter_modules`. A new file registers itself and appears in `|help`.

- **The `|` marker replaced bare-keyword matching.** The first version matched
  `status`/`grants`/`revoke` as plain text and used `startswith("revoke")`, so
  "revoke the old deploy key from GitHub" was answered with a usage error and never
  reached Claude. The marker makes intent explicit in both directions.
- **Any line starting with `|` means the message is not forwarded**, even when it
  fails to parse: a stray `|whatever` is a mistyped command, and passing it on
  would leak an operator instruction into what the agent sees. Every reply to a `|`
  message states that nothing was sent.
- **All local commands are operator-only**, including the read-only ones.
- `SlackParser` overrides `error()`, `exit()` **and `_print_message()`**. The first
  two would call `sys.exit` and kill the daemon; the third is the one that is easy
  to miss — argparse routes help to stdout through it, so without the override `-h`
  emitted the help twice and polluted the log.
- `-h` arrives as `CommandHelp` (a `CommandError` subclass) so it renders as usage
  rather than as a failure.
- `test_commands.py` covers the registry, argparse validation, `-h`, the
  never-forwarded rule, operator-only, and eight prose cases that must reach Claude.

## Slack rendering

- **Slack does not speak Markdown.** Claude writes GitHub-flavoured Markdown and
  Slack renders mrkdwn, which is a different language: `**bold**` shows literal
  asterisks, `## Heading` shows a literal `##`, `[text](url)` shows brackets, and a
  ```` ```lang ```` tag appears as the first line inside the block. `slackagent/mrkdwn.py`
  converts; `render.py` applies it to both the blocks and the fallback `text`.
- **Italic must be converted before bold.** The natural order is wrong: bold
  becomes `*x*`, which the italic rule then rewrites to `_x_`, so every bold word
  arrived italic. The italic pattern's lookarounds already refuse to match inside
  `**x**`, so running it first is safe. The test caught this immediately.
- **Nothing inside a fence or an inline code span may be rewritten.** Both routinely
  contain `*`, `_` and `[]`, and altering them misrepresents a command or a diff.
- **Tables are wrapped in a code fence**, because mrkdwn has no table syntax and
  alignment is the only reason a table was drawn.
- **Chunking must not split a fence.** A long message split mid-block leaves one
  chunk unterminated and the next starting with a stray ```` ``` ````, so Slack
  renders an endless code block followed by prose full of backticks. `_chunk`
  balances fences across chunk boundaries.

## Slack interaction gotchas

- **`respond()` replaces the original message by default.** A reply to a button's
  `response_url` needs `replace_original: False` or it destroys the message it came
  from. This actually happened: a guest clicked Approve, got the ephemeral refusal,
  and the click deleted the operator's buttons — leaving the request pending with no
  way to answer it, which any channel member could trigger at will. Every
  `respond()` payload in `approvals.py` now sets it, and `test_approvals` asserts
  it.
- **Recovery exists because a message can always be lost** — deleted by hand, or
  buried. `|pending` lists approvals with a live waiter and `--repost` posts fresh
  buttons. Only live waiters are listed: a waiter exists solely while the hook holds
  its HTTP request open, so after a timeout or a daemon restart there is nothing to
  answer and new buttons would be a lie.

## Who can do what## Slack rendering

- **Slack does not speak Markdown.** Claude writes GitHub-flavoured Markdown and
  Slack renders mrkdwn, which is a different language: `**bold**` shows literal
  asterisks, `## Heading` shows a literal `##`, `[text](url)` shows brackets, and a
  ```` ```lang ```` tag appears as the first line inside the block. `slackagent/mrkdwn.py`
  converts; `render.py` applies it to both the blocks and the fallback `text`.
- **Italic must be converted before bold.** The natural order is wrong: bold
  becomes `*x*`, which the italic rule then rewrites to `_x_`, so every bold word
  arrived italic. The italic pattern's lookarounds already refuse to match inside
  `**x**`, so running it first is safe. The test caught this immediately.
- **Nothing inside a fence or an inline code span may be rewritten.** Both routinely
  contain `*`, `_` and `[]`, and altering them misrepresents a command or a diff.
- **Tables are wrapped in a code fence**, because mrkdwn has no table syntax and
  alignment is the only reason a table was drawn.
- **Chunking must not split a fence.** A long message split mid-block leaves one
  chunk unterminated and the next starting with a stray ```` ``` ````, so Slack
  renders an endless code block followed by prose full of backticks. `_chunk`
  balances fences across chunk boundaries.

## Slack interaction gotchas

- **`respond()` replaces the original message by default.** A reply to a button's
  `response_url` needs `replace_original: False` or it destroys the message it came
  from. This actually happened: a guest clicked Approve, got the ephemeral refusal,
  and the click deleted the operator's buttons — leaving the request pending with no
  way to answer it, which any channel member could trigger at will. Every
  `respond()` payload in `approvals.py` now sets it, and `test_approvals` asserts
  it.
- **Recovery exists because a message can always be lost** — deleted by hand, or
  buried. `|pending` lists approvals with a live waiter and `--repost` posts fresh
  buttons. Only live waiters are listed: a waiter exists solely while the hook holds
  its HTTP request open, so after a timeout or a daemon restart there is nothing to
  answer and new buttons would be a lie.

## Who can do what

- **Requesting and approving are separate roles.** Anyone in a channel the bot is
  invited to may talk to it; only `AUTHORIZED_USER_ID` may press Approve/Deny or
  run `revoke`. Their approval message is labelled `requested by @them`, and
  `approvals` records `requested_by` alongside `resolved_by`.
- **DMs remain operator-only** on purpose: any workspace member can open a DM, so
  guest DMs would widen the audience from "people invited to a channel" to "the
  whole workspace".
- **The read exposure is the part to watch.** `Read`/`Grep`/`Glob` are auto-allowed,
  so anyone who can talk to the bot can read every file in `/home/agent/work`,
  private repos included, and have it printed into Slack. Approval buttons do not
  constrain this at all. `ALLOWED_USERS` narrows who can talk; tightening the
  auto-allow list to the workspace would narrow what they can read, and has not
  been done.
- Guests can also spend API budget, and a long thread costs more per turn (see the
  cache note above). There is no per-user rate limit.

## Bash policy (permissive)

Three modes: `open` (nothing asks), `permissive` (default), `strict` (every Bash
call asks). An unrecognised value falls back to `permissive`.

`open` is worth understanding precisely: it disables the gate, but the OS still
holds. The agent has no sudo and the hook, settings.json and agent-exec are
root-owned, so it cannot escalate or disable its own gate even then — the layering
is doing the work, not the policy. What `open` adds is `git push` with the forwarded
agent and self-modification of its dotfiles.

Set in the root-owned guest hook. The mode is chosen at runtime with `|auth`,
stored in the `settings` table, and read **per run** — so it takes effect on the
next message rather than the next restart, and a run in flight keeps what it
started with. `AGENT_POLICY` in `.env` is only the default for a database where
`|auth` has never been used. The daemon passes the mode in the job; `agent-exec`
logs which one ran.

- The premise: the VM is the boundary, not the gate. `Read`/`Grep` are already
  unrestricted so the agent can read every private repo here regardless, and the
  hook is root-owned so the policy cannot be edited by the identity it constrains.
  What is left worth a human is escalation, machine changes, state outside the VM,
  and writes outside the workspace.
- **Deny-list words are matched against every token**, not the head, so
  `sh -c "sudo id"` is caught. Matching whole commands by their first word would
  have missed it.
- **Benign paths are split into read-benign and write-benign.** With one list,
  `cp /tmp/x /usr/local/bin/agent-exec` read as harmless — the agent could have
  replaced the forced command confining it. `cp`, `install` and `tee` are mutators
  for the same reason; a redirect (`>`) also counts as mutating.
- Segment heads are taken across `&&`, `||`, `|` and `;`, so a mutator after a pipe
  is still seen.
- `test_bash_policy.py` is the record of where the line sits, in both directions:
  22 everyday commands that must run, and the escalation, push, dotfile,
  outside-workspace and gate-overwrite cases that must ask.

## Standing grants

- Grants are `(tool_name, prefix)` in the daemon's sqlite **on the host**, so the
  guest can only ask; it cannot grant itself anything. The check happens in
  `ApprovalService._handle_approve` before any Slack message is posted.
- Three match types: `prefix`, `exact`, `any`. The first version had only prefix,
  and refused any command containing a shell metacharacter — which in practice
  meant almost every real Bash call got no button at all, since agents constantly
  use `&&`, `|` and `>`. MCP tools got grants and the actual source of clicks did
  not.
- Compound commands are now segmented (quote-aware) and every segment must be
  covered independently, so granting `cd` and `npm test` covers
  `cd /x && npm test` while granting only `cd` does not cover `cd /x; rm -rf ~`.
  A mis-split can only add an uncovered segment, i.e. it fails towards asking.
- `exact` exists so that commands containing substitution or redirection are
  grantable at all: nothing can be appended to widen an exact match.
- Three classes are never offered as a prefix: substitution/redirection/newline,
  interpreters (`sh`, `python3`, `xargs`, `sudo`, `make`), and destructive commands
  (`rm`, `chmod`, `mv`, `systemctl`, `curl`, …). The destructive list exists
  because segmenting `git status; rm -rf /home/agent` would otherwise offer
  "Always allow: git status, rm" and one careless click would auto-approve every
  future `rm`.
- The word boundary is the second half: `git status` must not match
  `git statusfoo`. Match is exact-equal or prefix followed by a space.
- Tools with no subject field (`ToolSearch`, `mcp__*`, `TodoWrite`) get
  whole-tool grants with pattern `*`, which is safe because the tool name is the
  scope — a grant for `mcp__varys__pulse_command` does not cover
  `mcp__varys__pulse_reboot`. `MUST_BE_SCOPED` names the tools that may never be
  granted wholesale (`Bash`, the write tools, `WebFetch`), and `matches()` enforces
  it independently of `suggest_pattern`, so a hand-inserted wildcard row is still
  refused.
- Note an earlier claim in this file was wrong: the `approvals` table does NOT
  accommodate grants — it has no expiry, scope or pattern columns. `grants` is a
  separate table.

## Verified behaviour (don't re-derive these)

Checked against Claude Code 2.1.231 on terra:

- `--session-id <uuid>` works with `-p`, and the id echoes back in every
  event — so the daemon mints the UUID and writes the mapping *before* the
  CLI starts, rather than parsing it out of the init event.
- `--resume <uuid>` keeps the same session id and retains context.
- **`--session-id` is rejected for an id that already exists**:
  `Error: Session ID <uuid> is already in use.` The session is created on disk at
  the `system`/`init` event, so a thread must switch to `--resume` the moment
  init arrives — NOT when the run completes. Keying off `result` looks safer but
  is worse: a first run that emits init and then dies (dropped transport, killed
  ssh) would retry `--session-id` on every later message and break that thread
  permanently. `Store.mark_session_created` carries the reasoning; both
  directions are covered in `test_approvals.py`.
- **`--allowedTools` does not restrict the available tool set.** The init
  event still lists every tool. It pre-approves permissions; it is not a
  whitelist. The PreToolUse hook is therefore the *only* gate.
- The hook matcher must be `*`. With a per-tool matcher, a denied `Write`
  simply becomes a `Bash` redirect — observed in testing, the model went
  looking for exactly that route.
- Hook stdin carries `session_id`, `transcript_path`, `cwd`, `prompt_id`,
  `permission_mode`, `effort`, `hook_event_name`, `tool_name`, `tool_input`,
  `tool_use_id`.
- Hook stdout contract: `{"hookSpecificOutput": {"hookEventName":
  "PreToolUse", "permissionDecision": "allow"|"deny",
  "permissionDecisionReason": "..."}}`.
- A hook that errors or times out blocks the tool, so the gate is
  fail-closed by construction. It still emits an explicit deny so the model
  gets a readable reason.
- `$CLAUDE_PROJECT_DIR` did **not** expand in the hook command — use
  absolute paths in `settings.json`.
- The `result` event carries `permission_denials`, `total_cost_usd`,
  `duration_ms`, `num_turns`, `usage`, `is_error`.
- The stream also contains `rate_limit_event`; treat unknown event types as
  ignorable rather than errors.
- Pass `< /dev/null` to the CLI or it waits ~3s for piped stdin.
- **`virsh` defaults to `qemu:///session` for an unprivileged user**, which is a
  separate and empty hypervisor instance from `qemu:///system`. On terra after
  bootstrap: `virsh net-list --all` shows nothing while
  `virsh --connect qemu:///system net-list --all` shows `default` active. Every
  virsh call in this repo pins the URI explicitly; `tests/test_vmctl.py` guards
  it. Symptom if it regresses: the daemon reports "the VM is not running" on
  every turn regardless of the VM's actual state.
- **The agent must not be able to edit its own gate.** The first build put the
  hook script and the settings registering it in `/home/agent/.claude/`,
  agent-owned and agent-writable, and gave `agent` `NOPASSWD: ALL`. So a single
  approved `Write` deleted the gate for every future session — no sudo even
  needed — and sudo made root trivial on top. Now: the hook and its settings live
  in `/etc/claude-agent/` root-owned, `CLAUDE.md` is root-owned too (it is
  instruction, not workspace), and only `memory/` is agent-writable. There are
  two guest accounts — `agent` (no sudo, forced command only) and `admin` (sudo +
  shell, maintenance only, never used by the daemon). `verify-guest.sh` asserts
  the agent cannot write any gate file and has no sudo.
- **What the gate is and is not.** It is a guardrail against an agent doing
  something unhelpful, and an audit trail. It is not a sandbox against a
  determined adversary who already has code execution: the agent's cwd
  (`/home/agent/work`) is writable, so project-level Claude settings there are in
  reach, and one approved `Bash` call is arbitrary code as `agent`. The VM plus
  the egress policy are the real security boundary; the gate is the usability
  layer on top. Do not reason as though the gate alone contains a hostile agent.
- **The guest needs TWO keys.** The daemon key is pinned to a forced command,
  so it cannot open a shell or run rsync. With only that key and
  `lock_passwd: true`, a freshly provisioned VM is completely unreachable —
  no shell, and `virsh console` will not log in either. Learned the hard way:
  the first provisioning run produced exactly that, and the VM had to be
  destroyed and rebuilt. So: vm-files/ is embedded in the cloud-init seed
  (`render-cloud-init.py`) rather than pushed afterwards, and a separate
  `agent_vm_admin_ed25519` key gets a normal shell for `claude setup-token`
  and for later `30-install-vm-files.sh` updates.
- cloud-init runs `write-files` **before** `users-groups`, so
  `owner: agent:agent` on a write_files entry fails on a first boot. Files are
  staged root-owned under `/var/lib/agent-provision` and installed into place by
  a late `runcmd` instead.
- **`claude setup-token` hands back a token to export, not a credentials file.**
  It prints a `sk-ant-oat…` value for `CLAUDE_CODE_OAUTH_TOKEN`. Since a forced
  command reads neither ~/.bashrc nor ~/.profile, an exported variable never
  reaches agent-exec — it sources `~/.config/claude-agent/token` (0600,
  agent-owned) instead. `install-vm-token.sh` writes it with echo off, straight
  into the guest, so the value never lands on the host or in shell history. The
  token deliberately does NOT travel in the job from the host: the VM is the
  isolation boundary and owns its own credential.
- The guest has **no rsync** (and adding it would be a package the agent never
  otherwise needs), so `30-install-vm-files.sh` ships files with `tar` over ssh.
  Its chown needs `sudo`, because cloud-init leaves root-owned markers such as
  `.provisioned` in /home/agent.
- **A `systemctl --user` unit with mount-namespace sandboxing sees root-owned
  files as `nobody:nobody`.** `ProtectSystem`, `ProtectHome` and `PrivateTmp` each
  put the service in a user namespace where only the invoking uid maps; root maps
  to nobody. ssh validates the ownership of its config files, so it aborts with
  `Bad owner or permissions on /etc/ssh/ssh_config.d/20-systemd-ssh-proxy.conf`
  and exit 255 before opening any connection. `NoNewPrivileges` is fine — it
  creates no namespace. Fix: `ssh -F /dev/null`, which skips the user *and* the
  system-wide config (passing -F suppresses both). That is also the right default
  for a daemon, which should not inherit ambient host ssh config.
  **This class of bug is invisible outside the service** — every bridge test ran
  ssh from a normal shell and passed. `test_bridge_args.py` asserts the flag, and
  `systemd-run --user -p ProtectSystem=strict ...` reproduces the namespace
  cheaply when something looks fine by hand but fails as a unit.
- **`StartLimitIntervalSec` belongs in `[Unit]`, not `[Service]`.** systemd
  silently ignores it under `[Service]` (`Unknown key ... ignoring`), so the
  crash-loop protection it is meant to provide was never in effect. Check unit
  files with `systemd-analyze --user verify <path>` — it reports exactly this.
- **A missing `EnvironmentFile` fails the unit with "unavailable resources or
  another system error"**, which names nothing. Prefix it with `-` so the daemon
  starts and its own `Config.validate()` reports which variable is missing.
  Validate also rejects the unedited `.env.example` placeholders, because they are
  non-empty and correctly shaped, so the first symptom otherwise is an
  `invalid_auth` traceback from Slack.
- **`journalctl --user` does not work for tibber on terra**: `/var/log/journal` is
  `root:systemd-journal` and tibber is not in that group. The unit therefore also
  appends to `~/.local/share/slack-claude/daemon.log`, which is inside the one
  directory `ReadWritePaths` grants.
- **Never let a heredoc and a pipe both feed one ssh stdin.** In
  `printf token | ssh host 'bash -s' <<'EOF' ... EOF` the heredoc wins and the
  piped token is silently discarded — the remote `cat` reads an empty stream and
  installs a plausible-looking empty file. Put the remote commands in argv (they
  hold no secret; only the path) and reserve stdin for the payload. Then assert
  the result is non-empty, because the failure is otherwise invisible.
- **`mkdir -p` as root under `umask 077` creates `drwx------ root:root`.** The
  token file can be perfectly `-rw------- agent:agent` and still be unreachable,
  because the agent cannot traverse the parent directories — which surfaces in
  Slack as a bare "Not logged in" with no clue why. Use
  `install -d -o agent -g agent -m 0700` for each directory level, and verify
  with `sudo -u agent test -r` rather than by reading the file's own mode.
- **`sudo rm /dir/*` expands the glob as the CALLING user.** On a 0700 directory
  the caller cannot list it, so rm receives a literal `*`, and `-f` swallows the
  failure — the command reports success and deletes nothing. Use
  `sudo sh -c "rm -f /dir/*"` so root does the expansion. Same trap as every other
  identity bug here, wearing a different hat.
- **Claude Code's Bash tool strips the environment.** A command run through it
  reports `SSH_AUTH_SOCK` as UNSET even when the process that launched `claude`
  had it set — proven by asking the agent to echo it. So a forwarded ssh-agent is
  invisible to git by default. The fix is to not rely on the environment at all:
  agent-exec re-points `~/.ssh/agent.sock` at the forwarded socket on every run,
  and the agent's `~/.gitconfig` carries
  `core.sshCommand = SSH_AUTH_SOCK=/home/agent/.ssh/agent.sock ssh`.
  This bites tests too: anything run from a Claude Code Bash tool has no agent, so
  `ssh -A` from there forwards nothing. Export SSH_AUTH_SOCK explicitly when
  testing the forwarding path, or you will diagnose a working setup as broken.
- **ssh-agent destination constraints DO NOT work for git in the guest.** The
  intended design was `ssh-add -h "<vm>>git@github.com"` so a compromised guest
  could only aim the key at GitHub. It adds cleanly and then the agent hides the
  key. `ssh-agent -d` shows why: "1 socket bindings, 1 constraints", the single
  binding being the VM's hostkey. The inner ssh that git spawns is a separate
  client whose session-bind never joins the outer chain, so the agent sees `[vm]`
  while the constraint demands `[vm, github]`. Constraints are designed for one
  client traversing hops (ProxyJump); a nested ssh relayed through sshd does not
  accumulate. Two wrong theories were discarded first — GitHub *does* support
  `publickey-hostbound` (the `=<0>` is the version, and 0 is current), and the
  hostkeys did match.
  Consequence: forwarding is unconstrained, so **it is only safe with a read-only
  machine user.** With a personal account's key it forwards write access to every
  repo that account can write. `verify-agent-forwarding.sh` therefore parses the
  "Hi <user>!" banner and asserts the account name.
- **`ssh-add -h` rejects a user on the "from" hop.** The constraint is
  `<vm-host>>git@github.com`, not `agent@<vm-host>>git@github.com`; the latter
  fails with "cannot specify user on 'from' host" because the agent cannot verify
  the user at that point. Constraints also need `-H <known_hosts>` covering every
  hop, or ssh-add refuses to add the key at all.
- **A failing `ExecStartPost` tears down the main process.** ssh-agent looked like
  it was exiting 2 on its own; it was systemd killing it because load-agent-key.sh
  had failed. Read the *whole* status block, not just the ExecStart line.
- **`sudo git` in an agent-owned repo returns EMPTY, not an error.** git's
  `safe.directory` protection refuses a repo owned by another user, and
  `git config --get` then yields an empty string rather than failing visibly. A
  duplicate-clone guard built on `sudo git config --get remote.origin.url`
  therefore found no existing clone and cheerfully made a second one. Run every
  git read as the owner: `sudo -u agent -H git ...`. The earlier symptom
  "fatal: --local can only be used inside a git repository" was this same refusal.
- **`cmd | head` and `cmd | sed` hide the exit status.** A pipeline's status is the
  *last* command's, so `grep -q x | head || echo "not found"` never reports, and
  `git fetch | sed 's/^/ /' && echo OK` prints OK for a failed fetch. This produced
  three false readings in one session. Capture into a variable and check `$?`, or
  use `PIPESTATUS`.
- **My own request rate caused two "failures" today.** Rapid ssh retries exhausted
  the guest's `MaxStartups` (see above), and a burst of GitHub key auths — verify,
  three clones, several fetches, two `ssh -T` in about two minutes — produced a
  transient `Permission denied (publickey)` that cleared on its own with the key
  unchanged. When something that worked a minute ago stops working, consider the
  request rate before the configuration.
- **A remote `exit` only ends the remote shell.** `ssh host bash -s <<EOF ... exit 0`
  does not stop the local script, which then proceeds to verify work that was never
  done. Return a distinct code and branch on it locally.
- **Uninitialised submodules are EMPTY directories, not absent ones.** A clone
  without `--recurse-submodules` leaves real-looking but contentless components,
  so the agent reads the tree as complete and documents it wrongly — silently.
  `add-repo.sh` now counts them and warns with the list of repos required.
  `tibber-pulse-ir-hub-esp32` has 6 submodules across 6 distinct private repos.
- **Deploy keys do not scale to submodules.** GitHub allows one deploy key per
  repo, so an N-submodule tree needs N+1 keys *plus* `url.insteadOf` rewriting to
  map each `git@github.com:tibber/X` to the right Host alias. A single credential
  with read access to all of them (fine-grained PAT, or a machine user) is the
  right tool once submodules are involved.
- **`/home/agent` was `drwx------ agent:agent`** and is now `0711` with `work/` at
  `0755`, so the workspace is browsable over Tailscale without sudo. `.ssh`,
  `.config/claude-agent` and `.claude` keep `0700` and the token stays `0600` —
  verified by attempting to read it as `admin`, not by inspecting modes.
- **`/home/agent` traversal**, so the `admin` account cannot
  read or traverse it without sudo. **This has now caused five separate bugs** —
  in the verifier's stat, the setup-token instructions, the token-install probe,
  add-repo's existence check, and 30-install's verification block. When writing
  anything that runs over the admin key: every filesystem test, stat, find or
  `claude` invocation touching /home/agent needs `sudo` or `sudo -u agent -H`.
  Bare `[ -e ]` and `stat` do not error — they report "absent", which reads as a
  legitimate result and silently skips the check. Two consequences that both bit: anything
  admin runs against Claude Code needs `sudo -u agent -H`, and an unprivileged
  `stat` of paths under /home/agent returns *fewer lines* rather than an error —
  which is how two files silently went unchecked in a green verifier run. Use
  `sudo` and count what you checked.
- **An ssh forced command gets a non-login shell**, whose PATH on Debian is
  exactly `/usr/local/bin:/usr/bin:/bin:/usr/games` — no `~/.local/bin`, which is
  where the Claude Code native installer puts the binary. `agent-exec` therefore
  exports it explicitly and exits 69 with a readable log line if `claude` is
  still missing. Checking `claude --version` over an interactive login proves
  nothing about this; `verify-guest.sh` checks it in the right environment.
- **cloud-init runcmd order matters for ownership.** `mkdir -p` in runcmd runs as
  root, and `install -o agent` sets ownership on the files it writes but not on
  parent directories it creates. With `chown -R agent:agent /home/agent` placed
  after the Claude Code install, the installer died on
  `cannot create directory '/home/agent/.claude/downloads': Permission denied`
  while cloud-init still reported success. chown now runs *before* the install,
  and a `claude --version` step at the end makes the boot fail loudly instead of
  leaving a VM that looks fine but has no CLI.
- **Docker breaks libvirt NAT on this host.** `dockerd` sets the iptables
  FORWARD policy to DROP, and in netfilter a drop verdict is terminal across
  every base chain at the hook — so libvirt's own accept rules cannot rescue it.
  Symptom, which cost a build: the guest reaches `192.168.122.1` and resolves DNS
  (both host-local, INPUT hook) but every forwarded packet times out, so apt and
  the Claude Code install fail. Fix lives in `20-nftables-egress.sh`: ACCEPT
  `virbr0` in the `DOCKER-USER` chain, which Docker jumps to first and never
  flushes. LAN-deny still holds, because the `agentvm` chain runs at forward
  priority -10, ahead of Docker's table.
- **A forward-hook-only firewall does NOT contain the guest.** Traffic from the
  guest to one of the *host's own* addresses is host-local and hits the **input**
  hook, so a forward-only ruleset misses it entirely. Measured on terra with just
  the forward chain: forwarded LAN destinations (192.168.1.1, 192.168.2.1,
  10.0.0.1) were correctly blocked, but the guest could still reach
  `192.168.1.5` (terra's LAN IP) and its `tcp/22`, the VLAN address
  `192.168.10.200`, and terra's **tailscale0 address** — a route toward the whole
  tailnet. The partial success is what makes this easy to miss. `agentvm` now has
  an `input` chain allowing only DNS/DHCP to the gateway and dropping the rest.
  `bootstrap/verify-egress.sh` checks both properties, discovering the host's
  addresses from `ip addr` rather than hardcoding them.
- The input chain's `ct state established,related accept` is load-bearing: the
  daemon's SSH runs host->guest, so the guest's reply packets arrive on the input
  hook and would be dropped without it.
- **The guest is IPv4-only.** libvirt's default network has no IPv6 and the
  egress policy drops it, but Debian still returns AAAA first — so every fetch
  burned its connect timeout before falling back. cloud-init now writes
  `Acquire::ForceIPv4` and a `gai.conf` precedence rule.
- libvirt's dynamic ownership chowns disk images to `libvirt-qemu`, so removing
  them needs the `libvirt` group (the images dir is setgid `2775`).
- `virtiofsd` installs to `/usr/lib/virtiofsd`, not on `$PATH`. Unused by the
  current design (the VM clones its own repos) but there if virtio-fs ever
  returns.

## Working agreement for this repo

- When picking up work here, read this file first before making
  architectural suggestions — the high-level shape above is settled;
  focus on implementation.
- Update the "Still open" section as those items get resolved, rather than
  leaving stale open questions.
- `daemon/tests/run-all.sh --vm <ip>` adds `test_bridge_e2e.py`, which exercises
  the real tunnel against a live guest. It is the only test that proves the
  transport and the gate work *together*; run it after touching agent-exec, the
  hook, the bridge, or anything in cloud-init.
- Run `daemon/tests/run-all.sh` before committing. `test_gate_e2e.py` spends
  a little API usage but is the only test that proves the gate actually
  blocks a real Claude Code tool call.
