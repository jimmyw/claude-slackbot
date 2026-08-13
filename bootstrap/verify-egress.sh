#!/usr/bin/env bash
# Verify the agent VM's network containment from inside the guest.
#
#   ./bootstrap/verify-egress.sh <vm-ip>
#
# Unprivileged: connects with the admin key and runs the probes in the guest.
# Run after 20-nftables-egress.sh, and again after any host firewall change —
# Docker restarts and reboots both rewrite the FORWARD chain.
#
# Two properties must hold simultaneously, and it is easy to satisfy one while
# breaking the other:
#   1. the guest CAN reach the public internet (Anthropic API, git remotes)
#   2. the guest CANNOT reach anything on the LAN, on any of the host's own
#      addresses, or on the tailnet
#
# IMPORTANT: nftables cannot see inside WireGuard. If tailscaled runs in the
# guest, the nftables policy is no longer the control for anything reachable over
# the tailnet, and the ICMP probes below will pass while the guest can in fact
# reach tailnet peers. Section [6] checks for exactly that, because a verifier
# that reports green while the property is false is worse than no verifier.
set -euo pipefail

VM_HOST="${1:-}"
ADMIN_KEY="${ADMIN_KEY:-$HOME/.ssh/agent_vm_admin_ed25519}"

if [[ -z "$VM_HOST" ]]; then
    echo "usage: $0 <vm-ip>" >&2
    exit 64
fi

SSH=(ssh -i "$ADMIN_KEY" -o IdentitiesOnly=yes -o BatchMode=yes
     -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "admin@$VM_HOST")

# Every IPv4 address belonging to this host, plus its LAN neighbours and the
# gateway — discovered rather than hardcoded, so a new interface or VLAN on terra
# is covered automatically instead of silently escaping the check.
mapfile -t HOST_ADDRS < <(ip -o -4 addr show scope global \
    | awk '{split($4, a, "/"); print a[1]}' | sort -u)
mapfile -t NEIGHBOURS < <(ip -4 neigh show 2>/dev/null \
    | awk '/REACHABLE|STALE/ {print $1}' \
    | grep -v '^192\.168\.122\.' | sort -u | head -6)

echo "Host addresses to be denied:  ${HOST_ADDRS[*]:-none found}"
echo "LAN neighbours to be denied:  ${NEIGHBOURS[*]:-none found}"
echo

REMOTE_SCRIPT=$(cat <<'REMOTE'
set -u
fail=0
pass=0

report() { # name expected actual
    if [[ "$2" == "$3" ]]; then
        printf '  PASS  %-46s %s\n' "$1" "$3"; pass=$((pass+1))
    else
        printf '  FAIL  %-46s got %s, want %s\n' "$1" "$3" "$2"; fail=$((fail+1))
    fi
}

probe_tcp() { # host port -> open|closed
    if timeout 4 bash -c "cat </dev/null >/dev/tcp/$1/$2" 2>/dev/null; then
        echo open
    else
        echo closed
    fi
}

probe_ping() { # host -> up|down
    if ping -c1 -W2 "$1" >/dev/null 2>&1; then echo up; else echo down; fi
}

echo "[1] public egress must work"
for h in api.anthropic.com console.anthropic.com github.com; do
    code=$(curl -4 -sS --max-time 15 -o /dev/null -w '%{http_code}' "https://$h/" 2>/dev/null || echo 000)
    if [[ "$code" == "000" ]]; then
        printf '  FAIL  %-46s no response\n' "$h reachable"; fail=$((fail+1))
    else
        printf '  PASS  %-46s HTTP %s\n' "$h reachable" "$code"; pass=$((pass+1))
    fi
done
report "public ICMP (1.1.1.1)" up "$(probe_ping 1.1.1.1)"

echo "[2] DNS must work (host dnsmasq)"
if getent hosts api.anthropic.com >/dev/null 2>&1; then
    printf '  PASS  %-46s resolves\n' "DNS"; pass=$((pass+1))
else
    printf '  FAIL  %-46s no resolution\n' "DNS"; fail=$((fail+1))
fi

echo "[3] the host's own addresses must be denied"
for addr in HOST_ADDR_LIST; do
    report "host $addr icmp" down "$(probe_ping "$addr")"
    report "host $addr tcp/22" closed "$(probe_tcp "$addr" 22)"
done

echo "[4] LAN neighbours must be denied"
for addr in NEIGHBOUR_LIST; do
    report "lan $addr icmp" down "$(probe_ping "$addr")"
done

echo "[5] private ranges must be denied"
for addr in 192.168.1.1 10.0.0.1 172.16.0.1 169.254.169.254; do
    report "private $addr icmp" down "$(probe_ping "$addr")"
done

echo "[6] the guest must not have an independent tunnel out"
# WireGuard traffic leaves as UDP to a public endpoint (or DERP over 443), both of
# which the egress policy permits — by design, since that is indistinguishable
# from ordinary public egress. So a VPN inside the guest routes around LAN-deny
# entirely, and no amount of nftables checking will notice.
if command -v tailscale >/dev/null 2>&1 && sudo tailscale status >/dev/null 2>&1; then
    peers=$(sudo tailscale status --peers 2>/dev/null | awk 'NF>1 {print $1}' | grep -c '^100\.' || echo 0)
    printf '  FAIL  %-46s tailscaled is up with %s peers\n' "no independent tunnel out of the guest" "$peers"
    echo "        The nftables policy does NOT contain tailnet traffic. Reachability"
    echo "        is governed by your tailnet ACLs now, not by 20-nftables-egress.sh."
    sudo tailscale status --peers 2>/dev/null | head -4 | sed 's/^/        /'
    fail=$((fail+1))
else
    printf '  PASS  %-46s none found\n' "no independent tunnel out of the guest"
    pass=$((pass+1))
fi

echo
echo "passed=$pass failed=$fail"
exit $(( fail > 0 ? 1 : 0 ))
REMOTE
)

REMOTE_SCRIPT="${REMOTE_SCRIPT/HOST_ADDR_LIST/${HOST_ADDRS[*]:-}}"
REMOTE_SCRIPT="${REMOTE_SCRIPT/NEIGHBOUR_LIST/${NEIGHBOURS[*]:-}}"

if "${SSH[@]}" "bash -s" <<<"$REMOTE_SCRIPT"; then
    echo
    echo "Containment verified: public egress works, host and LAN are unreachable."
else
    echo
    echo "CONTAINMENT CHECK FAILED — see the FAIL lines above." >&2
    echo "If host addresses are reachable, the input chain in" >&2
    echo "20-nftables-egress.sh is missing or was flushed; re-run it as root." >&2
    exit 1
fi
