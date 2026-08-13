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

- Whether the agent should ever push. Currently no: deploy keys are read-only and
  the guest CLAUDE.md tells it not to try. Its git identity is set
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
- Scoped session-level approval grants ("approve Edit in this thread for
  10 minutes") to cut button fatigue. The `approvals` schema already
  accommodates it; deliberately not built for v1.

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
- **`/home/agent` is `drwx------ agent:agent`**, so the `admin` account cannot
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
