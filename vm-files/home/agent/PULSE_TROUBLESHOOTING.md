# Pulse Bridge troubleshooting

How to diagnose a Bridge in the field. **Part I** is the generic toolbox that applies to any
symptom. **Part II** holds one playbook per symptom — add new ones as cases come up, using the
template at the end.

> **No customer identifiers in this file.** Use placeholders (`<device-id>`, `<inverter-ip>`,
> `<qr>`). Real customer device data belongs in `test_customers.local.md` (local only) or Notion.

---

# Part I — Toolbox

## 1. Identifying a device

Three different ids refer to the same Bridge — mixing them up wastes time:

| Id | Looks like | Used by |
|---|---|---|
| Cloud/varys device id (32-hex UUID) | `4e2a…ea82` | `pulse_command`, `deploy_module.py`, varys URL |
| Device Id (12-hex) | `244cab…` | `version` output, telemetry `device_id` |
| QR code | `ABCD-1234` | **syslog hostname**, **esp-crash identifier**, web UI password |

- QR → device id: `varys pulse_lookup_qr`
- device id → QR: the `version` command's `burnin qr_code:` field

## 2. Getting a console

`pulse_command <device-id> "<cmd>" [wait_seconds]` sends a console command and returns whatever
appears on the device stream within the wait window.

Gotchas:

- **Don't pass `--log`** when you want the reply back — it routes output to the device *log*
  instead of the console, so you get nothing useful back.
- On devices actively running control schedules, telemetry frames flood the stream and crowd out
  the reply. Retry, raise `wait_seconds`, or accept that busy devices are noisy.
- `(no response received within timeout)` means either the device is offline **or** the command
  is slow/blocking (e.g. an upload). Re-check state with a cheap command like `version`.
- **Late output attaches to a later command's reply.** A long command (`ip_server_scan` over a full
  subnet, `coredump_upload`) can time out with no response, and its output then shows up prepended
  to the *next* command you send. So never conclude "that command produced nothing" — send a cheap
  `version` afterwards and read what comes back with it.
- Output written with `printf` from an **async callback** may never reach the varys stream at all.
  `ping` is the known case: it starts a session and returns, and its result callbacks print
  nothing you can see remotely (verified with a live gateway as a control). Treat `ping` as
  unusable over `pulse_command`.
- A missing field in output usually means the device runs an **older build** than the source you
  are reading. Check `version` before concluding a feature is broken.

## 3. Reading status telemetry

Any `[metric] status` frame (or `version`) gives the baseline. Read these first:

- `errors: [...]` — **error IDs, not counts** (Appendix A)
- `uptime` / `efr_uptime` — seconds; short uptime = recent restart
- `wificon` / `mqttcon`, `rssi` — connectivity (RSSI −40 good, below −75 suspect)
- `coredump_available: true` — a crash dump is stored (§6)
- `manifest_version` / `manifest_group` — release channel (§4)
- `modbus_servers[]` — Modbus peers and their error bits (Playbook A)

`version` also prints **`ESP reset reason:`** — `1` poweron, `3` SW, **`4` panic (crash)**,
`5` int WDT, `6` task WDT, `9` brownout. Reason 4 means the last restart was a crash, not a
user reboot — always worth following up (§6), even when it looks unrelated to the reported symptom.

## 4. Firmware version, release channel, and OTA

`ota_manifest` shows `manifest version <ver>[-<group>]`. Empty group = default (production)
channel; `…-beta` = beta. Manifest names are `<YYMM>.<n>` (e.g. `2604.3` = April 2026 rev 3), cut
from rolling lines like `stable_2604_1.manifest`.

Public manifests (no auth):

```bash
curl "https://tibber-firmware.s3.eu-west-1.amazonaws.com/tibber+bridge/TJH01/manifests/<name>.manifest"
```

Numbered snapshots (`2606.7.manifest`) are **not** publicly readable; rolling names
(`main.manifest`, `stable_2604_1.manifest`) are. Release announcements land in **#hardware-bot**.

### Watching an OTA

`ota_fetch` (writes), then poll `ota_manifest`. Per-model `state` goes
`needs_update` → `downloading` → `flashing` → `up2date`, with `current_version` catching up. The
ESP32 reboots after flashing — the console prompt changes (`esp32>` → `pulse-ir-hub-esp32>`),
a handy confirmation it restarted into the new build. EFR32 models update after the ESP32.

## 5. Mapping a version string to a commit

`Project Version: <build>-<sha8>`, where build = `git rev-list --count --first-parent <sha>`.

```bash
# does the deployed build contain commit X?
git merge-base --is-ancestor <X> <deployed-sha> && echo yes || echo no
# which build first contained it (find the merge, then count)
m=$(git rev-list --ancestry-path --merges <X>..origin/main | tail -1)
git rev-list --count --first-parent "$m"
```

**Author date is not merge order.** A commit authored before a build can still be absent from it
if it merged later. Always use `--is-ancestor`, never dates. (This trips people up: syslog was
authored 2026-04-07 but only merged 2026-04-27, so an April 20 build does *not* have it.)

## 6. Crash dumps (esp-crash)

The firmware uploads coredumps by itself — there is a **periodic 5-minute upload timer** to
`CONFIG_TIBBER_DEFAULT_COREDUMP_URL`, default **`https://esp-crash.wennlund.nu/dump`**. CI uploads
the matching ELF per build (`/upload_elf?project_name=<project>&project_ver=<version>`), so the
service can symbolize backtraces.

A dump is identified by `crash_identifier`:

```
ESP_CRASH:<PRODUCT_NAME>;<PROJECT_VER>;<QR code (or device id if no burnin)>;
```

So look a crash up by **QR code + the firmware version that was running when it crashed**.

> **After an OTA, search the *old* version.** The crash belongs to the build that was running at
> crash time, not the one now installed.

Console commands: `coredump_upload` (force an upload — can block long enough to time out the
console; verify afterwards rather than trusting silence), `coredump_erase`, `coredump_crash`
(deliberately crash — test rigs only). The device also serves the raw dump on its LAN webserver at
`/crash.dmp` (auth: password = QR code), useful when it's on the same network.

The web UI is behind GitHub OAuth, so it can't be read by automated fetches — a human needs to
open it.

## 7. Device logs via syslog

Requires **build ≥ 1518** (syslog client merged in `409d32f7`, 2026-04-27). On older builds
`param_get syslog_server` returns `0x2 (ERROR)` and remote logs are impossible — update first.

```
param_set syslog_server <host>:<port>     # host:port, validated
param_store                                # persist, or it's lost on reboot
log_set <tag> 4                             # raise verbosity if needed (persists in NVS)
```

Then query the syslog MCP with **`hostname` = the device QR code**:

```
query_logs(hostname="<qr>", app_name="<tag>", since="2h")
```

Gotchas:

- **The hostname may not parse.** On some builds the RFC 3164 header isn't parsed by the
  collector, so logs land under the **source IP** as hostname and `app_name` is sliced mid-string
  (`_link_task]`, `y_table`). If `hostname="<qr>"` returns nothing, run `list_hosts` and look for a
  raw-IP host that started right when you enabled syslog. Filter with `message_contains` rather
  than `app_name` in that case.
- Levels: `0` none, `1` error, `2` warn, `3` info, `4` debug, `5` verbose. Debug-level per-poll
  telemetry only reaches syslog if the tag's level allows it.
- Volume: a 1 Hz error loop is ~8k lines/hour, indefinitely. Turn syslog off when done.
- Syslog is UDP, facility `local0`.

---

# Part II — Symptom playbooks

## Playbook A — Modbus / HEMS: "modbus server shows error (2)"

**Symptom:** Varys shows a Modbus server with an error, and/or `errors: [2]` in telemetry.

### A.1 What the numbers mean

`errors: [2]` is error **ID** 2 = `ERROR_HANDLER_ERROR_MODBUS_TCP_CLIENT_NOT_ALL_SERVERS_CONNECTED`
(Appendix A) — the Bridge cannot open a TCP connection to a configured Modbus server. It is *not*
"2 errors".

`modbus_servers[].err` in telemetry, and `eb:` in `mb_server_list`, are a different thing: a
**bitmask** from `modbus_driver_server_connection_error_bits()` (Appendix B).

### A.2 The bitmask is sampling-dependent (known defect)

The getter early-returns bit 0 whenever the socket fd is inactive, *before* reading the stored bits:

```c
if ((p == NULL) || (p->tcp_sock_fd == INACTIVE_SOCKET)) {
    return MB_ERROR_SOCK_DISCONNECTED_BIT;   // hides the real reason
}
return p->sock_error_bits;
```

`cleanup_socket()` records the real cause **and** invalidates the fd in the same call, while
`sock_error_bits` is only cleared on a *successful* connect — and `tcp_sock_fd` is assigned by
`socket()` *before* the blocking `connect()`. Measured against an unreachable host, `connect()`
blocks **~30 s** on a **60 s** retry cycle, so for one and the same fault you see:

- `1` — sampled between attempts (fd inactive → early return)
- `2` — sampled during an attempt (fd valid, stale bit from the previous failure)

**Both mean "cannot connect".** A value flipping between 1 and 2 does not mean the fault changed.
Bits 4 and 16 are effectively unobservable for the same reason.

### A.3 Get the `errno` — this is the actual diagnosis

Enable syslog (§7) and read:

```
warning  Reconnecting to server <ip>
info     Connecting to <ip>:502 (mac:<mac>)
err      Failed to connect to '<ip>'. errno: 113
```

| errno | Name | Means | Points at |
|---|---|---|---|
| 113 | `EHOSTUNREACH` | no route / no ARP reply — nothing reachable at that address | peer powered off, off the network, on a different subnet, **or reachable-in-theory but blocked by network segmentation (§A.3.1)** |
| 111 | `ECONNREFUSED` | host answered, port closed | inverter is on the network; Modbus TCP disabled or wrong port |
| 110 | `ETIMEDOUT` | SYN sent, no answer | firewall / client isolation, or a flaky link |

**113 = network problem, 111 = Modbus configuration problem.** This one number decides whether to
ask the customer about the network or about inverter settings.

### A.3.1 Network segmentation — guest WiFi and client isolation

**Seen in the field, and the trap to avoid:** `113` does *not* prove the inverter is switched off.
The same errno appears when both devices are powered and online but cannot reach each other:

- **Either device on a guest network.** Guest SSIDs are normally a separate subnet/VLAN with no
  route to the main LAN. The Bridge on guest + inverter on main (or the reverse) fails exactly like
  an absent device.
- **AP/client isolation enabled.** Many routers — and most mesh extenders and repeaters, often by
  default — block client-to-client traffic on the same SSID. Both devices get valid addresses on
  the same subnet and still cannot talk to each other. This is the nastiest variant, because
  everything looks correct: same subnet, both online in their own apps, both reachable from the
  internet side.

How to tell it apart from a genuinely absent device:

- **Compare subnets.** Bridge `ip`/`gateway` in telemetry vs the inverter's configured IP. Different
  subnets (e.g. `192.168.1.x` vs `192.168.2.x`, or a `10.x` guest range) → segmentation, not a dead
  inverter.
- **Same subnet but still 113** → suspect client isolation, especially if the customer mentions a
  repeater/extender/mesh node, or the inverter connects over WiFi rather than cable.
- The Bridge's own health is irrelevant here: good `rssi` and a working cloud connection say
  nothing about whether it can reach another LAN client.

Worth asking the customer directly: *is either device on a guest network, and is there a WiFi
extender/mesh in the house?* Both are things they can check without visiting the inverter.
Moving both devices onto the same primary SSID, or disabling client isolation for it, is the fix.

Note this cuts both ways: **do not tell the customer to power-cycle the inverter** on the strength
of a 113 alone — if it's isolation, that wastes their time and confirms nothing.

### A.3.2 Sweep the subnet — `ip_server_scan`

The single most informative command for this symptom. It port-scans the local subnet and reports
every host that is listening, **with its MAC**:

```
ip_server_scan -n 192.168.1 -p 502 -s 1 -e 255 -t 200 -m 10
→ Scan results: 1 servers found on port:502 from 192.168.1.1 to 192.168.1.255
        [0]: 192.168.1.112   mac: '08:A6:F7:AF:F1:F7'
```

How to read the outcome:

| Result | Conclusion |
|---|---|
| A server at the **expected MAC**, different IP | IP changed; `ip_resolver` will adopt it within the hour (§A.4) |
| A server at a **different MAC** | the configured hardware is gone — see §A.4.1. Not self-healable |
| **No servers at all**, scan completes | nothing Modbus-capable on this subnet: peer absent, or segmentation (§A.3.1) |
| Scan itself finds *any* peer | **client isolation is ruled out** — client-to-client traffic works |

Notes:

- A full 1–255 sweep can take minutes and will blow past the console timeout. Either narrow the
  range (`-s 150 -e 160`) or send it, accept the timeout, and pick the output up on your next
  command (§2). Lower `-t` (per-host timeout, ms) to speed it up.
- `mdns_query` is a lighter-weight companion that queries for Modbus servers via mDNS.

### A.4 Self-healing already in the firmware

Error 2 registers `ERROR_HANDLER_ACTION_MODBUS_TCP_SERVER_IP_REFRESH` (`ip_resolver.c`): while set,
the Bridge rescans LAN hosts .1–.254 for the stored MAC, with a **1 hour grace period**. Reconnects
retry with exponential backoff capped at **60 s**.

So **if the inverter merely changed IP, the Bridge recovers on its own within about an hour.** An
error 2 persisting for many hours means the inverter isn't reachable at all. **Rebooting the Bridge
does not help** — say so early, it saves a pointless customer round-trip.

Note the grace period restarts on boot, so after an OTA or reboot the automatic rescan is ~1 h away.

### A.4.1 Stale MAC — a state that can never self-heal

`ip_resolver` matches **by MAC address**, not by IP or by "whatever is listening on 502". If the
peer's MAC changes, the Bridge searches forever for hardware that no longer exists, and no amount of
waiting, rebooting or power-cycling will fix it. Seen in the field: `ip_server_scan` found a Modbus
server at `.112` with a MAC whose OUI differed from the stored one, while the configured
`.154`/`68:25:DD:…` was absent.

Causes: a replaced communication module / WiFi dongle / comms stick, an inverter swap, or a service
visit — anything that changes network hardware gets a new MAC. (Or the server found is simply a
*different* Modbus device in the house — another inverter, charger or meter — and the original is
genuinely gone. Asking whether anything was swapped distinguishes the two.)

Signature: `errno 113` forever + `ip_server_scan` shows a Modbus server whose MAC ≠ the stored MAC.
Fix is to re-pair / repoint the stored target, not to restart anything.

**On builds carrying the `jimmyw/modbus_errno` change, the firmware says this itself** — no manual
scan needed. The resolver reports at the end of every sweep:

```
ip_resolver: Sweep of 192.168.1.1-254 complete: 7 hosts answered, none had MAC '<mac>'
(configured at <ip>). 3 sweep(s) in a row. The configured device is not on this subnet - ...
```

and the count is published as `mac_missing_sweeps` in telemetry and printed by `mb_server_list`:

```
[1]: <ip> - '<mac>' eb: 3 errno: 113 mac_missing_sweeps: 3
```

Any non-zero `mac_missing_sweeps` means the LAN was swept and the configured MAC is not on it →
§A.4.1, repoint rather than restart. It clears automatically once the MAC reappears or the servers
reconnect.

### A.5 Steps

1. `version` → build, reset reason, QR. Old build? Consider updating first (§4).
2. Status frame → `errors: [2]` present?
3. `mb_server_list` → which IP/MAC, and `eb:` (`1` or `2` both mean cannot connect).
4. Check `rssi`/`ip` — if the Bridge itself has a good WiFi link, the fault is on the peer side.
5. Persisted > ~1 h? The IP rescan has already failed → the peer is not on the LAN.
6. Enable syslog and read the `errno` (§A.3) → decide network vs configuration.
7. On `113`, before blaming the inverter: compare the Bridge's subnet with the inverter's IP, and
   ask about guest networks and WiFi extenders (§A.3.1). Segmentation looks identical to an absent
   device.
8. Ask the customer accordingly: inverter powered and on the network? Both devices on the *same*
   (non-guest) SSID? Modbus TCP enabled? Any recent network change — new router, new WiFi, mesh
   extender, guest network?

### A.6 Open engineering items

- ~~`modbus_driver_server_connection_error_bits()` masking the stored bits; `connect()` `errno` not
  surfaced~~ — **done**, branch `jimmyw/modbus_errno` (driver + contracts + hub, unpushed). Bits no
  longer masked (a failed connect reports `DISCONNECTED|CONNECT_ERROR` = 3), `errno` published as
  `ModbusServerInfo.last_errno` and printed by `mb_server_list`.
- ~~MAC-mismatch visibility~~ — **done**, same branch. See §A.4.1.
- **Cloud/app side still open:** map `last_errno` and `mac_missing_sweeps` to human text in Varys and
  the app. The firmware now reports the facts; nothing renders them yet.
- Consider a fallback when the MAC is absent but exactly one Modbus server exists on the subnet —
  currently the resolver only ever matches on MAC, so recovery needs a human.
- **Port the ECU's foreground `ping`** (`tibber-battery-esp32/components/cmd_ping/cmd_ping.c`, which
  has `-b/--background` and blocks by default) into the hub firmware, ideally switching its
  `printf`s to `ESP_LOG` so results reach syslog as well as the console.
- Reported correlation (Vidar): coredumps are often present on devices where Modbus is broken.
  Unconfirmed — needs a coredump decoded per §6 to see whether the crash is in the
  Modbus/smartlink path.

## Playbook B — EFR32 not responding (`errors: [1]`)

*Stub — fill in when a case comes up.* Error ID 1 =
`ERROR_HANDLER_ERROR_EFR_NOT_RESPONDING`: the ESP32 is not getting answers from the EFR32 radio
over UART. Start from `version` (`Efr32 hub version`), `efr_uptime` in telemetry, and
`log_set efr_hub 4`.

## Playbook C — Repeated crashes / panics

*Stub.* Entry points: `ESP reset reason: 4` and `coredump_available` (§3), decode via esp-crash
(§6), and note the reset-counter interaction — OTA is refused while the reset counter is non-zero
to avoid a WDT cascade, so a crash-looping device can also get stuck on an old build.

## Playbook D — Device offline / not reaching cloud

*Stub.* Entry points: `wificon`, `mqttcon`, `rssi`, `ip`/`gateway`/`dns*` in telemetry;
`(no response received within timeout)` from `pulse_command`.

## Playbook E — Pulse CT paired and online, but no CT data

**Symptom:** the node shows up in `nodes` and sends metrics, the app shows "last seen \<hours
ago\>" and no consumption. Typically reported right after a firmware update ("it worked before
the update").

### E.1 What the numbers mean

Everything lives on the **EFR32**, reached over the ESP32 console as
`efr_cli clamp_info <node-id>` (node id from `efr_cli nodes`, e.g. `1`):

| Field | Meaning | Source |
|---|---|---|
| `clamp_active_channels_bitfield` | channels the hub believes carry current | `node_db.h` |
| `line_certainty` | consecutive agreeing detections; **5** (`REQ_LINE_CERTAINTY`) = "calibrated", persisted to NVM3 | `clamp_line_detection.h:11` |
| `clamp_line.N` | voltage line assigned to clamp channel N; **255** = `LINE_NUMBER_NOT_FOUND` | `clamp_line_detection.c:21` |
| `data.reading.N.rms` | current in amps as reported by the node | wire message |
| `data.reading.N.zero_cross_index` | **65535** = node found no zero crossing (`UNKNOWN_ZERO_CROSS_INDEX`, node-side sentinel is `UINT16_MAX` — the hub's own `UNKNOWN_ZERO_CROSS` is `UINT32_MAX`, different constant, don't compare them) | node `meter_clamp_helper.c:47` |
| `data.reading.N.error_bits` | see Appendix D | `clamp_reading.h:32-36` |

Two thresholds drive everything (`src/clamp_reading.h:26`, `clamp_line_detection.c:23`):

- **0.2 A** (`ACTIVE_CHANNEL_CURRENT_THRESHOLD_A`) — below this a channel is not even *active*.
- **4 A** (`line_detect_current_threshold_min_a`, param, per-node override) — below this an active
  channel can't have its line detected (`METER_CLAMP_ERROR_BIT_LINE_DETECT_NEED_MORE_CURRENT`).

### E.2 Known traps

- **`data.signature` prints negative.** `uint32_t` printed with `%ld` in `src/app_cli.c:739`
  (same for `zero_cross_index` at `:743`). Cosmetic — reinterpret as unsigned.
- **`line_certainty: 5` does not mean the device is calibrated.** With zero active channels the
  detector certifies an empty result — see E.6. Always read `clamp_line.*` alongside it: certainty
  5 with `clamp_line.0/1/2 = 255` is the *broken* state, not a calibrated one.
- **`clamp_line.3/4/5 = 0` on a 3-channel node is not data.** Only `entries` (3) channels are
  written; the rest are the zeroed struct. `[255,255,255,0,0,0]` therefore also says the node
  entry was created fresh (re-pair / new node), not loaded from a previously good calibration.
- **`CLAMPS_NOT_CONNECTED` (bit 4) absent ≠ clamps are on the wire.** That bit only checks the DC
  bias of the analog input, i.e. that the CT is plugged into the node. A CT dangling next to the
  conductor looks perfectly "connected".
- **`clamp_set_lines` is not a probe — it permanently pins the node.** It sets
  `line_certainty = 255` *and* `clamp_active_channels_bitfield = 0xFF` and stores
  (`app_cli.c:605`). Certainty is then above `REQ_LINE_CERTAINTY` and every reading's active bits
  are trivially within `0xFF`, so `needs_evaulation()` never fires again and **auto-detection is
  dead on that node until someone runs `clamp_detect`**. It also marks all 6 channels active on a
  3-channel node, so `clamp_info` afterwards reports 6 clamps and drops the `UNCERTAIN_PHASE`
  flags. Take the diagnostic dump *before* anyone reaches for it — afterwards the device can
  neither reproduce a detection bug nor validate a fix.

### E.3 Get the real diagnosis

The question is always *"is the node measuring current at all?"*

```
efr_cli clamp_info <node-id>
```

- `rms` ≥ 4 A and `zero_cross_index` sane → measurement is fine, the problem is line assignment.
- `rms` ≈ 0.0x A on all channels **and** `error_bits: 2` (`INVALID_ZERO_CROSS`) → the node sees
  no AC at all. Everything else (255s, bitfield 0) is downstream of this. Do not chase the line
  detection; chase the current.

Consequence upstream: with `clamp_line = 255` the hub sends `clamp_line = (int8_t)-1` per channel
plus `METER_CLAMP_ERROR_BIT_UNCERTAIN_PHASE`, which the ESP32 maps to
`PHASE_OFFSET_DEGREE_UNSPECIFIED` (`components/iot_efr/pulse_data_handler_dispatch_efr.c:247`).
The cloud gets frames carrying ~0 A and no phase → nothing to show in the app.

### E.4 What the firmware does by itself

- Re-detection is triggered only by `clamp_line_detection_needs_evaulation()`
  (`clamp_line_detection.c:66`): certainty below 5, **or** the reading's active channels not being
  a subset of the stored ones. `CHECK_BITS_WITHIN(0, x)` is always true, so a node reading zero
  current never re-triggers — but the moment any channel exceeds 0.2 A,
  `CHECK_BITS_WITHIN(0x7, 0)` is false and detection restarts. **It self-heals once real current
  flows**, so "wait for load" is a legitimate step, and re-pairing is not required.
- Nothing restores a previously good line map. Once the 255s are in NVM3 they stay until a
  successful detection overwrites them.

### E.5 Steps

1. `efr_cli nodes` — confirm the node is alive (`seen` small) and note whether `ota` says
   `distributing`; a node mid-OTA is a moving target, let it finish.
2. `efr_cli clamp_info <node-id>` — classify per E.3.
3. If `rms` ≈ 0: ask the customer to verify the clamps are **closed around the phase conductors**
   (not the incoming cable as a bundle, not left hanging), and that there is real load.
4. Confirm the node fw actually changed: the `version` column in `nodes` is `<count>-<sha8>`; map
   it to a commit per §5. "It broke on the update" is only actionable if the node really updated.
5. With ≥4 A on the clamped conductors, force a fresh detection:
   `efr_cli clamp_detect <node-id> [threshold*0.1A]` (resets certainty, polarity, active bitfield
   and stores). **Do not run this on an unloaded installation** — with no load it re-certifies the
   empty result immediately (E.6) and you are back where you started.
6. Manual override if the phases are known: `efr_cli clamp_set_lines <node-id> <ch0..ch5>` and
   `efr_cli clamp_set_active_channels <node-id> <bitfield>`. Note the stickiness in E.2 — and that
   this is also a *useful experiment*: pinning the lines removes line assignment as a variable, so
   whatever `rms` comes back afterwards is the node's honest measurement. Still ~0 A → the node
   isn't measuring, and the line map was never the blocker. Real amps → measurement is fine and
   the fault was purely line detection.

### E.6 Open engineering items

**Bug — line detection certifies an empty result when no channel is active** (hub EFR32,
`src/clamp_line_detection.c`). `found_all_lines()` (`:185`) skips inactive channels, so with
`active_channels_bitfield == 0` its loop body never runs and it returns `true`.
`clamp_readings_are_valid()` (`clamp_reading.c:223`) skips them the same way, so the
`INVALID_ZERO_CROSS` errors don't block detection either. `evaluate_line_certainty()` then stores
the all-`255` guess (`update_node_with_new_line_detection_results`, `:224`), and because every
following reading agrees, certainty climbs to `REQ_LINE_CERTAINTY` and `save_to_flash()` persists
`clamp_lines = [255,255,255,…]`, `clamp_active_channels_bitfield = 0`, `line_certainty = 5`.

**Introduced in `cca02a21` — "Pu 468 move all phase calculations to efr hub" (#77), 2024-05-08,
build 716.** That PR moved phase calculation from the node to the hub and invented
`active_channels_bitfield`, `save_to_flash()` and the certainty counter in one go. Before it,
`found_all_lines()` checked *every* channel, so an all-255 guess always returned false and nothing
was ever persisted. Adding the `continue` for inactive channels — correct on its own — made the
"no channel active" case vacuously true, and the same PR added the flash write that makes it
stick. The whole defect landed atomically; there is no window where one half existed without the
other.

Exposure (checked against the published manifests, `<count>-<sha8>` → commit per §5):

| Group | Manifest | hub-efr32 (fg1 + fg23) | Bug |
|---|---|---|---|
| `main` | `main_0.0.74` | `816-1e86863d` | **yes** |
| `jj_bredge_main` (edge) | `jj_bredge_main_0.0.8` | `814-495de619` | **yes** |
| `stable_2509_1` | `2509.1` | `799-f289b4a8` | **yes** |
| `stable_2506_1` | `2604.1` | `795-379a5e21` | **yes** |
| `stable_2505_1` | `2506.2` | `795-379a5e21` | **yes** |
| `stable_2403_2` | `2411.1` | `724-62cfacc2` | **yes** |
| `stable_2312_1` | `2402.2` | `672-e2f82c6f` | no (predates #77) |

So every published bridge manifest except `stable_2312_1` ships it — it has been in the field
since the 2403 line (branch `stable_2403_1`, 2024-09-02 onward; `stable_2312_1` is the last clean
branch). It stayed invisible because it only fires on a CT that reads below 0.2 A on *every*
channel while detection is pending, and it self-heals as soon as real current appears.

Reproduced as a host unit test (`test/test_clamp_line_detection/`, case
`..._no_active_channels_must_not_certify`), which failed against `main` with:

```
detect_lines returned 1 1 1 1 1 1 | certainty=6 bitfield=0 lines=[255,255,255,0,0,0] needs_eval=0
(I): Node 0: Phases found! Saving to flash... [255, 255, 255, 0, 0, 0]
```

— identical to the field dump. Existing tests cover "active but under 4 A"
(`..._low_current_fail`) but not "nothing active at all".

**Fix (applied, not yet on a branch):** `found_all_lines()` now tracks whether it saw any active
channel and returns that instead of an unconditional `true`, so "nothing to find" is no longer
"found everything". Certainty stays 0, no bogus map reaches flash, and `clamp_info` keeps telling
the truth about an uncalibrated node. Fixing it there rather than early-returning in
`clamp_line_detection_detect_lines_internal()` closes the trap for every caller.

Instrumentation fixed alongside it:
- `%ld` → `%lu` on the two `uint32_t` fields in `src/app_cli.c:739,743` (target build is clean
  under `-Werror`, confirming `uint32_t` is `unsigned long` there).
- `save_to_flash()` now logs the active-channel bitfield and certainty next to the line map, so a
  `[255, 0, 255, …]` map reads as "channels 1 only" instead of looking like a failed detection.

## Template for a new playbook

```markdown
## Playbook <X> — <symptom as support would describe it>

**Symptom:** what is seen in Varys / the app / telemetry.

### <X>.1 What the numbers mean      — decode ids/fields, link to appendices
### <X>.2 Known traps                — misleading values, sampling issues, version differences
### <X>.3 Get the real diagnosis     — the log line / command that names the true cause
### <X>.4 What the firmware does by itself  — retries, timeouts, self-healing, so we don't
                                              recommend actions that can't help
### <X>.5 Steps                      — numbered, cheapest first, ending in a customer-facing ask
### <X>.6 Open engineering items     — bugs/instrumentation gaps found while diagnosing
```

---

# Appendices

## Appendix A — Device error IDs (`errors: []`)

From `components/project_config/pulse_config.h.in`. There are only two:

| ID | Constant | Meaning |
|---|---|---|
| 1 | `ERROR_HANDLER_ERROR_EFR_NOT_RESPONDING` | EFR32 radio not answering |
| 2 | `ERROR_HANDLER_ERROR_MODBUS_TCP_CLIENT_NOT_ALL_SERVERS_CONNECTED` | cannot open TCP to a configured Modbus server |

## Appendix B — Modbus error bits (`err` / `eb:`)

| Bit | Value | Constant | Meaning |
|---|---|---|---|
| 0 | 1 | `MB_ERROR_SOCK_DISCONNECTED_BIT` | socket not open |
| 1 | 2 | `MB_ERROR_SOCK_CONNECT_ERROR_BIT` | TCP connect failed |
| 2 | 4 | `MB_ERROR_SOCK_SEND_ERROR_BIT` | send failed |
| 3 | 8 | `MB_ERROR_SOCK_RECV_TIMEOUT_BIT` | receive timed out |
| 4 | 16 | `MB_ERROR_MAC_ADDRESS_MISMATCH_BIT` | MAC at that IP isn't the expected one (newer drivers) |

See §A.2 before trusting these values.

## Appendix C — Console command reference

Read-only unless marked.

| Command | Purpose |
|---|---|
| `version` | firmware version, Device Id, QR code, reset reason, coredump flag |
| `param_get <name>` | read a param (`0x2 (ERROR)` = param doesn't exist in this build) |
| `param_set <name> <v>` | **writes** — set a param |
| `param_store` | **writes** — persist params to NVS (required to survive reboot) |
| `log_list [filter]` | show per-tag log levels |
| `log_set <tag> <0-5>` | **writes** — set a tag's level (persists in NVS) |
| `log_reset <level>` | **writes** — reset *all* tags |
| `ota_manifest` | manifest, per-model versions and OTA state |
| `ota_fetch` | **writes** — re-fetch manifest, start pending updates |
| `mb_server_list` | Modbus servers + `eb:` bits (`eb:` only on newer builds) |
| `ip_server_scan -n <subnet> -p <port> [-s <start> -e <end> -t <ms> -m <max>]` | sweep the subnet for listeners, reports IP **and MAC** (§A.3.2) |
| `mdns_query` | query for Modbus servers via mDNS |
| `ping <host>` | **hub firmware: unusable remotely** — async, prints via `printf`, reaches neither varys nor syslog. The ECU fork has a foreground version (`-b` to background) |
| `mb_server_add` / `mb_server_save` / `mb_server_clear` | **writes** — edit the server table |
| `coredump_upload` | **writes** — force a coredump upload (may block past the console timeout) |
| `coredump_erase` | **writes** — discard the stored dump |
| `mod_list` | loaded runtime modules (edge drivers) |

## Appendix D — Clamp error bits (`clamp_info` `error_bits`)

From `tibber-pulse-ir-hub-efr32/src/clamp_reading.h` (mirrored in the node's
`products/clamp/meter_clamp.h` and in `shared_components/efr/wire_protocol_clamp_data.h`).

| Bit | Value | Constant | Set by | Meaning |
|---|---|---|---|---|
| 0 | 1 | `METER_CLAMP_ERROR_BIT_TIME_SYNC_INVALID` | node | time sync to the hub not valid |
| 1 | 2 | `METER_CLAMP_ERROR_BIT_INVALID_ZERO_CROSS` | node | no zero crossing found — no measurable AC |
| 2 | 4 | `METER_CLAMP_ERROR_BIT_UNCERTAIN_PHASE` | hub | channel not in `clamp_active_channels_bitfield` |
| 3 | 8 | `METER_CLAMP_ERROR_BIT_LINE_DETECT_NEED_MORE_CURRENT` | hub | active channel below the 4 A line-detect threshold |
| 4 | 16 | `METER_CLAMP_ERROR_BIT_CLAMPS_NOT_CONNECTED` | node | CT not plugged into the node (DC bias check only) |
| 6 | 64 | `METER_CLAMP_ERROR_BIT_LOW_CURRENT` | ECU | — |
