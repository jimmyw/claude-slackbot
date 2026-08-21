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
  `groups:history`, `im:history`, `im:read`, `im:write`, `assistant:write`
  - `chat:write` also covers deleting the bot's own placeholder
  - the `*:history` scopes are what `conversations.replies` needs for the catch-up
    transcript; without them a tagged bot cannot see what it missed
  - `users:read` and `reactions:*` are granted on the live app but deliberately
    **unused** — messages are labelled with Slack ids, not names, so no profile data
    leaves the workspace
- **Event subscriptions**: `app_mention`, `message.channels`, `message.groups`,
  `message.im` — `message.im` is not optional: DMs arrive on it, and without it the
  bot looks dead in its own DM
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

**Three modes per thread.** All three are set in the thread they apply to, stored in
sqlite so they survive a restart, and lifted with `|resume`. Other threads are
unaffected, and the thread's Claude session is never closed — resuming continues the
same conversation with its context.

| | untagged reply | tagged / DM | cost of a message it ignores |
|---|---|---|---|
| default | forwarded; it judges for itself | answered | one turn |
| `\|silent` | dropped by the daemon | answered, told what it missed | nothing |
| `\|pause` | dropped | dropped | nothing |

`|silent` is the useful middle: the bot stays out of the conversation but stays
reachable. Only a real @-tag or a DM gets through — typing its name as plain text does
not — and everything else is dropped out here on a rule rather than in there on
judgement, so an ignored message costs nothing at all.

`|silent` sounds stricter than `|pause` and is in fact looser, so the transitions say
what they did: `|silent` in a paused thread announces that it lifted the pause, and
`|resume` names which of the two it lifted. `|status` reports the mode, who set it,
when, and how many messages it has dropped since — there are now three reasons the bot
might be quiet, and they look identical in Slack.

**It knows who is talking.** Every message reaches the agent labelled with the
writer's Slack id (`<@U013P2T2ZHT>: shall we ship?`), and it is told to address the
person it is answering when more than one person is in the thread. Ids rather than
names: Slack renders an id as the person's name for whoever reads the reply and the
same token pings them, so no `users.info` call is made and no profile data leaves the
workspace. Note what this grants — the agent can now notify people, which it could
not before.

**It catches up on what it missed.** When you tag it after a gap, the daemon fetches
the messages it was not shown and quotes them, oldest first, each labelled with its
author — so "can you look at that?" is answerable. That includes being tagged into a
thread it has never spoken in: the first mention brings the conversation so far with
it, which is what makes "@bot can you fix this?" work in a thread that has been
running without it. Set `CATCH_UP_NEW_THREADS=0` to have it see only what is said
after it is tagged. Bounded at 20 messages, 600 characters each, 6000 in total,
newest kept, and it states what it left out rather than truncating quietly — a long
thread costs the same as a long gap. A `|pause` window is never quoted, because
`|pause` promised those messages were never seen. Quoted text is fenced and the agent
is told it is context, never instruction and never permission.

**MCP credentials live on the host, not in the VM.** An MCP server configured inside the
guest carries its credential there, outside both the VM boundary and the approval gate —
one approved Bash call is arbitrary code as `agent`. Instead the guest runs a small relay
that reaches a proxy in the daemon over a second reverse-forwarded loopback port, open
only for the lifetime of a run. The proxy holds the credentials, decides what may be
called, and writes the audit trail.

Because the proxy resolves the run token to the Slack user who sent the message, the
guest cannot claim to be someone else — so a server can present **per-user credentials**
(each person's own token upstream) or a shared one, and policy can differ per person. A
tool nobody has allowed is filtered out of the tool list entirely, so the agent never
sees it. Set it up with `MCP_CONFIG` and `bootstrap/60-migrate-mcp.sh`; with no host
config the VM keeps its own MCP setup, unchanged.

**It stays quiet when it wasn't asked.** Anyone can reply in a thread the bot
owns, and most of those replies are people talking to each other. Those messages
are handed to the agent with a note saying nobody mentioned it; if it decides the
message wasn't for it, it answers with `[[no-reply]]` and the daemon posts nothing
at all — no message, no "working…", no trace. Nothing is deferred for a mention or
a DM: those are unambiguously for the bot, so it answers and shows its working as
before. Silence is logged (`staying silent: …`) so it stays diagnosable, and
transport failures are still posted either way.

**Permissive by default.** Most work runs without interrupting you:

| | asks? |
|---|---|
| reading anything — `Read`, `Grep`, `Glob` | no |
| writing inside `/home/agent/work` | no |
| ordinary shell — build, test, git, `curl`, package installs in the workspace | no |
| escalation or machine changes — `sudo`, `systemctl`, `apt`, `mount`, `dd`, `nft`, `crontab`, `modprobe`, `tailscale` | **yes** |
| state outside the VM — `git push`, `git remote set-url`, `git config --global`, `npm publish` | **yes** |
| writing outside the workspace — `/etc`, `/usr/local/bin`, anything under `/home/agent` that isn't `work/` | **yes** |
| its own configuration — `~/.ssh`, `~/.gitconfig`, `~/.claude`, `~/.bashrc` | **yes** |

The reasoning: the VM is the security boundary, not this gate. Egress is
outbound-only with the LAN denied, no credential is at rest, and the hook is
root-owned so the agent cannot edit the policy that constrains it. `Read` is
already unrestricted, so the agent can read every repo here regardless — what
remains worth a human is anything that changes the machine, escalates, or reaches
back out of the workspace.

Three modes, most open first — `|auth` lists them:

| mode | behaviour |
|---|---|
| `open` | nothing is ever asked; the gate is off |
| `permissive` | the table above: ordinary work runs, escalation and reach outside the workspace ask |
| `strict` | every `Bash` call asks |

**`open` is not as total as it sounds, and that is deliberate.** What protects the
VM is the operating system, not this switch: the agent has no sudo, and the hook,
its `settings.json` and `agent-exec` are all root-owned, so even with the gate off
it cannot escalate or disable its own gate. What `open` newly permits is `git push`
using the forwarded ssh-agent — with a personal key that means anything you can
write — and edits to its own `~/.gitconfig`, `~/.claude` and `~/.ssh`, which
persist between runs. `|auth open` says so at the moment you choose it.

Switch mode from Slack with `|auth <mode>` — no restart. The
choice is stored in the daemon's sqlite and read **per run**, so it applies from
your next message; a run already in flight keeps the policy it started with,
because the policy travels with the job. `AGENT_POLICY` in `daemon/.env` is only
the default for a database that has never had `|auth` used on it.

The policy is enforced by the root-owned hook in the guest, so the agent cannot
alter it in either mode — `|auth` only selects which mode the hook runs in, and
`agent-exec` logs which one each run used.

The matching is deliberately crude — it inspects tokens, not shell semantics — so
it errs towards asking. Two cases from `daemon/tests/test_bash_policy.py` show why
the detail matters:

- `sh -c "sudo id"` asks. The deny-list is matched against every word in the
  command, not just the first, so hiding a command inside an interpreter does not
  get past it.
- `cp /tmp/x /usr/local/bin/agent-exec` asks. `/usr/local/bin` is harmless to
  *read* and very much not to *write*, so the benign-path list is split by whether
  the command mutates. Without that split, the agent could replace the forced
  command that confines it — which is what the test caught.

Workspace writes use `os.path.realpath`, so `work/../../etc/passwd` and a symlink
planted at `work/escape -> /etc` both fall through to asking rather than being
treated as inside the workspace. `daemon/tests/test_hook_paths.py` covers those escapes with
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

**It can notify people now.** Messages reach the agent labelled with the writer's
Slack id, and it is told to address the person it is answering — so it can write
`<@U…>`, which pings a real phone. Nothing in the daemon constrains who: the only
control is the instruction in the guest `CLAUDE.md` not to tag anyone who is not
already in the conversation. It also means the messages other people exchange in a
thread are forwarded to the API when the bot is tagged after a gap, not just the ones
addressed to it — and with `CATCH_UP_NEW_THREADS` on (the default) that includes the
history of a thread it is tagged into, said before anyone knew it would be. Bounded,
but a real widening of what leaves the workspace; `CATCH_UP_NEW_THREADS=0` narrows it
back to what was said after the tag. No names or profile data go with them.

**Only `AUTHORIZED_USER_ID` can decide.** Anyone in the channel can see the
buttons; a click from anyone else gets an ephemeral refusal naming the operator and
leaves the request pending.

> A reply to a Slack button's `response_url` **replaces the original message
> unless you pass `replace_original: False`**. Without it, an unauthorized click
> deleted the approver's buttons and left the request pending and unanswerable —
> a denial of service any channel member could trigger by clicking. Fixed, and
> `|pending --repost` recovers an approval whose message is lost for any other
> reason. `revoke` is operator-only too — a guest changing what
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
|status               VM state, SSH bridge, policy, grants, MCP, this thread's mode
|mcp                  MCP servers, who may call what, recent calls
|mcp tools <server>   ask an upstream live; marks each tool allowed or blocked
|mcp allow <s> <glob> permit a tool from the next message
|mcp calls            the MCP audit trail
|silent               only answer in this thread when tagged
|pause                answer nothing at all in this thread
|resume               back to normal (lifts either one)
|auth                 list all modes, marking the current one
|auth open            nothing asks at all
|auth permissive      the default
|auth strict          ask for every Bash call
|pending              approvals still waiting for you
|pending --repost     post their buttons again
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
- `test_silence.py` — the three modes and the three kinds of quiet: a dropped message
  starts no run at all (asserted on the prompt, not just on what was posted), a
  mention in a silent thread does, a muted thread posts no refusals, and a bare
  mention still wakes it.
- `test_thread_modes.py` — the mode store and the migration off `paused_threads`,
  including the half-migrated shape a `RENAME` implementation would never reach.
- `test_attribution.py` — prompt assembly and its injection defences, the catch-up
  filter and its bounds, a failing and a hanging `conversations.replies`, and that
  `|resume` from a pause does not backfill it.
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
