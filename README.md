# Slack-controlled Claude Code daemon

A Claude Code session runs headless in an isolated VM on `terra`. You drive it
from Slack; unsafe tool calls wait for you to press Approve. The agent keeps
notes in markdown so it remembers things between threads.

```
  Slack ──socket mode──▶ daemon (terra, systemd --user)
                            │
                            │  ssh -R <port>:127.0.0.1:9100  agent@vm
                            │  forced command: /usr/local/bin/agent-exec
                            ▼
                    ┌──────────────────────────────────────┐
                    │ VM: claude -p --output-format         │
                    │     stream-json                       │
                    │        │ stdout ─── events ──────────┼──▶ Slack message
                    │        │                              │    (edited live)
                    │  PreToolUse hook ──POST──▶ :<port> ───┼──┐
                    └──────────────────────────────────────┘  │ reverse tunnel
                            ▲                                  │
                            └── approval listener ◀────────────┘
                                 posts Approve/Deny to Slack
```

The VM never holds the Slack token and never talks to Slack. The approval hook
reaches the daemon through the reverse tunnel that the daemon itself opened, and
that tunnel exists only for the lifetime of the run — which is exactly when the
hook can fire.

## Layout

```
bootstrap/
  00-host-packages.sh      privileged; you run this once
  10-provision-vm.sh       creates the VM (unprivileged, after 00)
  20-nftables-egress.sh    privileged; LAN-deny egress policy
  30-install-vm-files.sh   pushes vm-files/ UPDATES to a running guest
  verify-guest.sh          checks a provisioned guest is correct
  verify-egress.sh         checks public egress works and LAN/host do not
  cloud-init/user-data     guest provisioning template
  render-cloud-init.py     embeds vm-files/ + both pubkeys into the seed
daemon/
  slackagent/              the daemon
  tests/                   see "Tests" below
  systemd/                 the --user unit
vm-files/                  everything that lives inside the guest
```

## Setup

### 1. Host bootstrap (you, with sudo)

```sh
sudo ./bootstrap/00-host-packages.sh
```

Installs `qemu-base`, `edk2-ovmf`, `dnsmasq`, `cloud-image-utils`; enables
`libvirtd`; starts the `default` NAT network; puts `/var/lib/libvirt/images` on
its own ZFS dataset and makes it group-writable by `libvirt`; adds you to
`libvirt`/`kvm`; and enables systemd lingering so the daemon survives a reboot.

It is idempotent — safe to re-run.

**Log out and back in** before continuing — the group has to be active in your
shell.

> **`virsh` gotcha.** For an unprivileged user, bare `virsh` resolves to
> `qemu:///session`, a *separate and empty* hypervisor instance. Everything here
> lives in `qemu:///system`, so `virsh list --all` will look empty while the VM is
> running fine. Either pass `--connect qemu:///system` or
> `export LIBVIRT_DEFAULT_URI=qemu:///system` in your shell. The daemon and the
> provisioning script both pin it explicitly.

### 2. Slack app (you, in a browser)

Create an app at <https://api.slack.com/apps>:

- **Socket Mode**: on → gives you an app-level token (`xapp-…`)
- **Bot token scopes**: `app_mentions:read`, `chat:write`, `channels:history`,
  `groups:history`
- **Event subscriptions**: `app_mention`, `message.channels`
- Install to the workspace → bot token (`xoxb-…`)
- Your user ID: Slack profile → More → Copy member ID (`U…`)

### 3. Provision the VM

```sh
./bootstrap/10-provision-vm.sh              # prints the VM's IP when done
```

This generates **two** SSH keys and embeds all of `vm-files/` into the cloud-init
seed, so the guest is fully provisioned on first boot:

- `~/.ssh/agent_vm_ed25519` → the **`agent`** account: no sudo, pinned to a
  forced command. No shell, no rsync. This is all the daemon ever uses.
- `~/.ssh/agent_vm_admin_ed25519` → the **`admin`** account: shell + sudo, for
  maintenance and `claude setup-token`. Never used by the daemon.

Two accounts on purpose. With one shared account, the agent inherited `admin`'s
`NOPASSWD: ALL`, so any single approved `Bash` call was a root shell — and root
could rewrite the approval gate. The gate's own files
(`/etc/claude-agent/approve.py`, `settings.json`) are root-owned for the same
reason: the identity a control constrains must not be able to edit that control.

The files are embedded rather than pushed afterwards because on a fresh VM there
is nothing to push *with*: the daemon key can't open a shell, and `lock_passwd:
true` means `virsh console` can't log in either. Get that wrong and the VM is
unreachable and has to be rebuilt.

Wait for cloud-init (it installs Claude Code), lock down egress, then authenticate:

```sh
ssh -i ~/.ssh/agent_vm_admin_ed25519 admin@<vm-ip> 'cloud-init status --wait'
sudo ./bootstrap/20-nftables-egress.sh

# From a real terminal (setup-token needs a TTY):
ssh -t -i ~/.ssh/agent_vm_admin_ed25519 admin@<vm-ip>
PATH=/home/agent/.local/bin:$PATH claude setup-token
```

`setup-token` prints a token rather than writing a credentials file, and a forced
command reads neither `~/.bashrc` nor `~/.profile` — so an exported variable never
reaches `agent-exec`. Install it into the guest with:

```sh
./bootstrap/install-vm-token.sh <vm-ip>     # prompts with echo off
```

It goes to `/home/agent/.config/claude-agent/token`, mode 0600, agent-owned, and
never touches the host disk or your shell history. **Do not paste the token into
a chat or a terminal that logs** — if you have, revoke it in the Anthropic console
and issue a new one.

`30-install-vm-files.sh <vm-ip>` pushes later changes to `vm-files/`; it isn't
needed for a fresh build.

### 4. The daemon

```sh
cd daemon
python3 -m venv .venv && .venv/bin/pip install -e .
cp ../.env.example .env && chmod 600 .env    # fill in tokens, VM_HOST, user ID
mkdir -p ~/.local/share/slack-claude

mkdir -p ~/.config/systemd/user
cp systemd/slack-claude-daemon.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now slack-claude-daemon
journalctl --user -u slack-claude-daemon -f
```

Invite the bot to a channel and mention it. Replies in the thread continue the
same Claude session.

## How it behaves

**Threads are sessions.** Mentioning the bot starts a new Claude session; every
reply in that thread resumes it. The daemon mints the session UUID itself and
writes the mapping before the CLI starts, so the mapping is durable even if the
first run dies. One `asyncio.Lock` per thread keeps two fast replies from
resuming the same session concurrently.

**Read-only tools run freely** — `Read`, `Grep`, `Glob`, `TodoWrite`,
`NotebookRead`. Everything else, `Bash` very much included, waits for you.

`Bash` is deliberately *not* auto-allowed. A gate that blocks `Write` but allows
`Bash` blocks nothing, since `bash -c 'echo > file'` does the same job — and in
testing the model went looking for exactly that route when a `Write` was denied.

**Only `AUTHORIZED_USER_ID` can decide.** Anyone in the channel can see the
buttons; a click from anyone else gets an ephemeral refusal and leaves the
request pending. Silence for `APPROVAL_TIMEOUT_S` (default 600) is a denial.
Every decision lands in the `approvals` table with who and when.

**`status` in a thread** reports the VM state and whether the SSH bridge answers.

## Tests

```sh
cd daemon
./tests/run-all.sh
```

- `test_approvals.py` — the gate, with a fake Slack: authorized approve,
  unauthorized click, timeout-denies, unknown run token, double click, run dying
  under a pending approval, and the thread↔session mapping.
- `test_render.py` — replays **real** `stream-json` captured from Claude Code
  2.1.231 (`tests/fixtures/`), plus Slack's block/length limits and the
  transport-failure paths.
- `test_vmctl.py` — guards the `qemu:///session` trap above: every `virsh` call
  must pin `--connect`, plus state mapping and `domifaddr` parsing.
- `test_gate_e2e.py` — the one that matters: a **real** `claude -p` run through
  the real `agent-exec` and the real hook, with only SSH and the button click
  stubbed. Approve creates the file; Deny does not, and the denial shows up in
  the result event. Spends a little API usage.

`--offline` skips only the last one (no API usage, no Claude auth needed).

## Verification after setup

1. **Host** — with `export LIBVIRT_DEFAULT_URI=qemu:///system` (see the gotcha
   above), `virsh list --all` works without sudo and `virsh net-list` shows
   `default` as active/autostart; `loginctl show-user tibber -p Linger` →
   `Linger=yes`.
2. **Guest** — `virsh domifaddr agent-vm` gives an IP. The daemon key is refused
   a shell but runs the forced command:
   ```sh
   ssh -i ~/.ssh/agent_vm_ed25519 agent@<ip>          # expect: exit 64, "empty job"
   ```
3. **Guest provisioning** — `./bootstrap/verify-guest.sh <vm-ip>` checks
   cloud-init finished, the tooling and Claude Code are installed and reachable
   *in a non-login shell*, the embedded files have the right owners and modes,
   the approval hook fails closed, and the daemon key cannot get a shell.
4. **Egress and containment** — one script checks both halves:
   ```sh
   ./bootstrap/verify-egress.sh <vm-ip>
   ```
   Public egress must work while the host's own addresses, its LAN neighbours and
   the private ranges are all unreachable. It discovers terra's addresses from
   `ip addr`, so a new VLAN or interface is covered automatically. Re-run it after
   any Docker restart or reboot — both rewrite the FORWARD chain.
5. **Loop** — mention the bot; the reply streams in. Reply in-thread; it shows it
   kept context. `sqlite3 ~/.local/share/slack-claude/state.sqlite3 'select * from threads'`
   shows one row with a stable `session_id`.
6. **Gate** — ask it to write a file. Buttons appear. **Click as a second Slack
   user → refused, still pending.** Click as yourself → the write goes through.
   Repeat and let it time out → denied, and `select state from approvals` reads
   `timeout`.
7. **Lifecycle** — `systemctl --user restart slack-claude-daemon`, then a Slack
   message still works. Reboot terra: VM autostarts, daemon autostarts, the bot
   answers with no manual step.
8. **Memory** — ask it to remember something. Start a **new** thread and ask
   about it; it should recall from `memory/MEMORY.md`.

## What the approval gate is, and is not

It is a guardrail against the agent doing something unhelpful, plus an audit
trail of every decision. It is **not** a sandbox against a determined adversary
that already has code execution: one approved `Bash` call is arbitrary code as
`agent`, and its working directory is writable. The VM boundary and the egress
policy are the real security controls. The gate makes an autonomous agent safe to
supervise; it does not make a hostile one safe to run.

## Hardening (not done yet)

- **Egress is LAN-deny, not an allowlist.** `20-nftables-egress.sh` blocks the VM
  from every RFC1918 destination (forward hook) *and* from every address on terra
  itself, including `tailscale0` (input hook — host-local traffic never reaches
  the forward hook, which is how the first version leaked). It does not restrict
  which *public* hosts the VM reaches. A CONNECT proxy with a hostname allowlist is the tighter version;
  with `git clone` and package installs in scope it is also a maintenance
  treadmill, so it is deliberately deferred.
- **Snapshots.** Put the VM disk on a dedicated ZFS dataset and add a timer
  running `zfs snapshot ssd/vm@auto-$(date +%F)` **with rotation** — `ssd` is at
  75% capacity, so an unbounded timer would eat the remaining 201 GB.
- **Approval fatigue.** A twenty-write task means twenty button presses. The fix
  is a scoped session-level grant; the `approvals` schema already accommodates
  it.

## Operational notes

- A dropped Socket Mode connection mid-run leaves a stream with no reader. The
  daemon reports transport failures into the thread rather than leaving a
  placeholder hanging, but a run interrupted this way is not resumed
  automatically — reply in the thread to continue.
- `APPROVAL_TIMEOUT_S` must stay below the hook `timeout` in
  `vm-files/home/agent/.claude/settings.json` (900s). Otherwise the harness kills
  the hook before Slack can answer and the deny reason becomes a hook crash
  instead of "nobody answered". `Config.validate()` enforces the gap.
- Tunnel ports come from a pool (`TUNNEL_PORT_LOW`–`HIGH`). Without distinct
  guest ports, a second concurrent run fails to bind and silently rides the
  first run's tunnel, which then dies underneath it.
