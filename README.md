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

Add your own keys to `bootstrap/operator-keys.pub` (public keys, safe to commit,
one per line) and they are installed on `admin` at every build. Do **not** put a
key on the `agent` account: a shell there shares the forwarded ssh-agent with
whatever you run and can read the agent's Claude token. `test_cloud_init.py`
asserts that.

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

# From a real terminal — setup-token needs a TTY, so this cannot be piped.
ssh -t -i ~/.ssh/agent_vm_admin_ed25519 admin@<vm-ip>

# Then, in that session, run it AS THE AGENT USER. /home/agent is drwx------
# agent:agent, so admin cannot reach the binary at all — `claude` on admin's own
# PATH is "command not found" no matter what you prepend. sudo keeps the TTY.
sudo -u agent -H bash -c 'export PATH=$HOME/.local/bin:$PATH; claude setup-token'
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

# journalctl --user does not work for tibber on terra (/var/log/journal is
# root:systemd-journal), so the unit also appends to a file:
tail -f ~/.local/share/slack-claude/daemon.log
```

If the unit fails with "unavailable resources or another system error", that is
almost always a path in the unit that does not exist. `systemd-analyze --user
verify ~/.config/systemd/user/slack-claude-daemon.service` names the problem.

Invite the bot to a channel and mention it. Replies in the thread continue the
same Claude session.

## How it behaves

**Threads are sessions.** Mentioning the bot starts a new Claude session; every
reply in that thread resumes it. The daemon mints the session UUID itself and
writes the mapping before the CLI starts, so the mapping is durable even if the
first run dies. One `asyncio.Lock` per thread keeps two fast replies from
resuming the same session concurrently.

**Three tiers of approval.**

1. **Read-only tools run freely** — `Read`, `Grep`, `Glob`, `TodoWrite`,
   `NotebookRead`.
2. **Writes inside `/home/agent/work` run freely** — `Write`, `Edit`,
   `MultiEdit`, `NotebookEdit`. Documentation work is dozens of files, and a
   button press each made it unusable; the git diff is the better review anyway.
3. **Everything else waits for you** — `Bash` above all, network fetches, and any
   write whose path resolves outside the workspace.

`Bash` is deliberately never auto-allowed. A gate that blocks `Write` but allows
`Bash` blocks nothing, since `bash -c 'echo > file'` does the same job — and in
testing the model went looking for exactly that route when a `Write` was denied.

Tier 2 uses `os.path.realpath`, so `work/../../etc/passwd` and a symlink planted
at `work/escape -> /etc` both fall through to tier 3 rather than being treated as
inside the workspace. `daemon/tests/test_hook_paths.py` covers those escapes with
real symlinks, and `30-install-vm-files.sh` re-checks them in the guest on every
push.

## Working with repos

```sh
# Public repo:
./bootstrap/add-repo.sh <vm-ip> https://github.com/owner/repo.git

# Private repo, read-only:
./bootstrap/make-deploy-key.sh <vm-ip> owner/repo   # prints a key to add on GitHub
./bootstrap/add-repo.sh <vm-ip> git@github.com-repo:owner/repo.git repo
```

`add-repo.sh` clones **as the agent user**, which is the whole point: a clone made
by `admin` or over your own Tailscale login leaves the tree owned by that user,
and since `agent` has no sudo it then cannot write a single file. The symptom is
the agent reporting permission errors on work it was just asked to do, over a
clone that looks perfectly healthy. The script verifies ownership, writability,
and that no file in the tree belongs to anyone else.

One deploy key per repo — GitHub refuses to accept the same key on a second
repository — so each gets an SSH `Host` alias and you clone via the alias.

The agent can commit locally but cannot push: the deploy keys are read-only, and
its `CLAUDE.md` tells it not to waste a gated call trying.

### Who can do what

| | talk to it | approve | manage grants |
|---|---|---|---|
| `AUTHORIZED_USER_ID` | yes, channels and DM | **yes** | yes |
| anyone in a channel it's invited to | yes | no | no |
| anyone else | no | no | no |

**Anyone in a channel the bot is invited to can talk to it** — the invite is the
grant. Their requests run, and anything not pre-approved asks *you*, labelled
`requested by @them`. The `approvals` table records both who asked
(`requested_by`) and who decided (`resolved_by`).

**Read this before inviting the bot anywhere:** `Read`, `Grep` and `Glob` run with
**no approval at all**, so anyone who can talk to the bot can read any file in
`/home/agent/work` and have it printed into Slack — every cloned private repo
included. Letting someone talk to the bot is granting them read access to that
workspace. Narrow it with `ALLOWED_USERS` if that is not what you want.

**DMs stay with the operator.** Any workspace member can open a DM with the bot, so
allowing guest DMs would mean the whole workspace rather than the people you
deliberately invited. Guests are told to use a channel.

**Only `AUTHORIZED_USER_ID` can decide.** Anyone in the channel can see the
buttons; a click from anyone else gets an ephemeral refusal naming the operator and
leaves the request pending. `revoke` is operator-only too — a guest changing what
runs unattended would defeat the point. Silence for `APPROVAL_TIMEOUT_S`
(default 600) is a denial.

### Standing grants ("always allow")

Approvals carry a third button. What it offers depends on what can be generalised
safely:

```
🔒 Bash — git status --short
   [Approve]  [Always allow: git status]  [Deny]

🔒 Bash — cd /home/agent/work/repo && npm test
   [Approve]  [Always allow: cd, npm test]  [Deny]          ← creates two grants

🔒 Bash — echo hello > notes.md
   [Approve]  [Always allow this exact command]  [Deny]

🔒 mcp__varys__pulse_command
   [Approve]  [Always allow all mcp__varys__pulse_command]  [Deny]
```

```
grants        list them with use counts
revoke 3      remove one
revoke all    clear them
```

Grants live in the daemon's sqlite **on the host**, so the agent can only ask — it
can never grant itself anything.

**Compound commands need every segment granted.** `cd /x && npm test` is covered
only when both `cd` and `npm test` are. Granting just `cd` does not make
`cd /x; rm -rf ~` acceptable. Splitting is quote-aware, so `echo "a && b"` stays
one segment, and a mis-split can only ever add a segment nothing covers — it fails
towards asking you.

**Three things are never offered as a prefix**, because a prefix would generalise
from one sighting to everything of that shape:

| | why | offered instead |
|---|---|---|
| substitution, redirection, newlines — `$( )`, `` ` ``, `>`, `<` | the effect isn't determined by the opening words | exact |
| interpreters — `sh`, `python3`, `xargs`, `sudo`, `make` | a prefix on these is a prefix on everything they can run | exact |
| destructive — `rm`, `chmod`, `mv`, `systemctl`, `curl`, `wget` … | one careless click would auto-approve every future `rm` | exact |

That last one is why `git status; rm -rf /home/agent` offers only *this exact
command* rather than `Always allow: git status, rm`.

**And these tools can never be granted wholesale**, because their name doesn't
bound them: `Bash`, `Write`/`Edit`/`MultiEdit`/`NotebookEdit` (an approval means a
path *outside* the auto-allowed workspace, including `~/.gitconfig`, whose
`core.sshCommand` the forwarded ssh-agent depends on), and `WebFetch` (an
arbitrary outbound URL is an exfiltration channel). `covered_by()` re-checks this,
so even a wildcard row inserted by hand is refused at match time.

### Local commands

A message whose text starts with `|` is a **local command**: the daemon answers it
itself, and it is never forwarded to Claude. Operator only.

```
|help                 list the commands, with a one-line description each
|status               VM state, whether the SSH bridge answers, grant count
|grants               standing grants with ids and use counts
|grants --tool Bash   only that tool's grants
|grants --unused      only grants that have never matched
|revoke 3             remove one grant
|revoke all           remove every grant
```

Each command has its own help:

```
|revoke -h
  usage: |revoke [-h] target

  Remove a standing grant so its tool calls ask for approval again.

  positional arguments:
    target      the grant id from |grants, or 'all' to remove every grant

  Examples:  |revoke 3   |revoke all
```

**A line starting with `|` never reaches Claude, even when it doesn't parse.**
`|grnats` gets an error listing the available commands; it is not passed on as a
prompt. Every reply to a `|` message says so explicitly, so a typo can never
quietly become a request. That also means the old keywords are free again:

| message | goes where |
|---|---|
| `|revoke 3` | the daemon |
| `revoke the old deploy key from GitHub` | **Claude** |
| `|status` | the daemon |
| `status of the build?` | **Claude** |

#### Adding a command

One module per command in `daemon/slackagent/commands/`, discovered automatically —
a new file registers itself and appears in `|help` with no list to update:

```python
NAME = "snapshot"
ALIASES = ()
SUMMARY = "take a ZFS snapshot of the VM disk"

def build_parser() -> argparse.ArgumentParser:
    parser = SlackParser(prog="|snapshot", description="…")
    parser.add_argument("--tag")
    return parser

async def run(ctx: Context, args: argparse.Namespace) -> None:
    await ctx.say("…")
```

Parsing is real `argparse`, with two of its habits neutralised: `error()` and
`exit()` normally print to stdout/stderr and call `sys.exit`, which in a daemon
would kill the process and tell the operator nothing. `SlackParser` turns both into
exceptions and suppresses argparse's own output — without that, `-h` emitted help
twice, once into the log.

Everything else you say — including a message that merely mentions these words —
becomes a Claude Code turn in that thread.

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
- `test_grants.py` — "always allow" matching, weighted towards bypasses:
  chaining, pipes, redirection, substitution, newline injection, line
  continuation, and the `git statusfoo` boundary.
- `test_cloud_init.py` — asserts the guest's security invariants in the rendered
  seed: the `agent` account has exactly one key pinned to the forced command and
  no sudo, operator keys land on `admin` and never on `agent`, the gate's files
  install root-owned, `MEMORY.md` installs agent-owned, and no secret appears in
  the seed. Two of those had already been broken once.
- `test_vmctl.py` — guards the `qemu:///session` trap above: every `virsh` call
  must pin `--connect`, plus state mapping and `domifaddr` parsing.
- `test_gate_e2e.py` — the one that matters: a **real** `claude -p` run through
  the real `agent-exec` and the real hook, with only SSH and the button click
  stubbed. Approve creates the file; Deny does not, and the denial shows up in
  the result event. Spends a little API usage.

- `test_bridge_e2e.py` — the whole chain against a **live VM**, nothing stubbed
  but Slack: daemon `Bridge` → `ssh -R` → `agent-exec` → `claude -p` → the hook →
  back through the reverse tunnel → approval → the file really is or is not
  created in the guest. Opt in with `./tests/run-all.sh --vm <vm-ip>`; needs a
  provisioned, authenticated VM and spends real API usage.

`--offline` skips the API-spending suites (no Claude auth needed).

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
