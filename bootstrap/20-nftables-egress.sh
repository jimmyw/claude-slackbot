#!/usr/bin/env bash
# Egress policy for the agent VM.
#
#   sudo ./bootstrap/20-nftables-egress.sh
#
# What this enforces: the VM may reach the public internet (it needs
# api.anthropic.com, console.anthropic.com for token refresh, and whatever git
# remotes and package registries its work requires), but it may NOT reach any
# other host on terra's LAN. Lateral movement into the home network is the real
# threat from a box running an autonomous agent; this closes it.
#
# What this deliberately does NOT do: hostname-level allowlisting. With git
# clones and package installs in scope, a CONNECT-proxy allowlist becomes a
# maintenance treadmill. See the README for the proxy upgrade path if you later
# want to tighten this.
#
# Note the VM's own DNS goes to the libvirt gateway (dnsmasq on virbr0), which is
# host-local traffic on the INPUT hook — the forward rules below never see it.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "must run as root: sudo $0" >&2
    exit 1
fi

VM_SUBNET="${VM_SUBNET:-192.168.122.0/24}"
BRIDGE="${BRIDGE:-virbr0}"
CONF=/etc/nftables-agentvm.conf

cat > "$CONF" <<EOF
#!/usr/bin/nft -f
# Managed by slackbot/bootstrap/20-nftables-egress.sh — do not hand-edit.
table inet agentvm
delete table inet agentvm

table inet agentvm {
    chain forward {
        # priority -10 puts this ahead of libvirt's own filter rules (priority 0)
        type filter hook forward priority -10; policy accept;

        ct state established,related accept

        # Deny the VM every RFC1918 / link-local destination. Public egress is
        # still permitted by the accept policy.
        ip saddr $VM_SUBNET ip daddr {
            10.0.0.0/8,
            172.16.0.0/12,
            192.168.0.0/16,
            169.254.0.0/16,
            100.64.0.0/10
        } counter log prefix "agentvm-lan-deny " drop

        # No IPv6 egress at all — the allowlist story is simpler with one family.
        ip6 saddr fe80::/10 counter drop
    }

    chain input {
        # Traffic from the guest to one of the HOST's own addresses is host-local:
        # it hits the input hook, not forward, so the chain above never sees it.
        # Without this chain the guest could reach every service on terra —
        # measured on this box: sshd on 192.168.1.5:22, the VLAN address
        # 192.168.10.200, and terra's tailscale0 address, which is a route toward
        # the whole tailnet. Forwarded LAN destinations were correctly blocked,
        # which is exactly what made the gap easy to miss.
        type filter hook input priority -10; policy accept;

        ct state established,related accept

        # The only host services the guest legitimately needs: DNS and DHCP from
        # the libvirt dnsmasq on the gateway. DHCP DISCOVER comes from 0.0.0.0,
        # so this cannot be narrowed by source address.
        iifname "$BRIDGE" udp dport { 53, 67 } accept
        iifname "$BRIDGE" tcp dport 53 accept

        # Everything else the guest aims at this host is denied, on every
        # interface and address family.
        iifname "$BRIDGE" counter log prefix "agentvm-host-deny " drop
    }
}
EOF

chmod 0644 "$CONF"
nft -f "$CONF"

# --- Docker coexistence -----------------------------------------------------
#
# Docker sets the iptables FORWARD policy to DROP. In netfilter a drop verdict is
# terminal across every base chain at the hook, so libvirt's own accept rules
# cannot override it — guest traffic reaches Docker's FORWARD chain, matches
# nothing, and dies on the policy. Symptom: the VM can reach 192.168.122.1 and
# resolve DNS (both host-local) but every forwarded packet times out.
#
# DOCKER-USER exists for exactly this. Docker's FORWARD chain jumps there first
# and never flushes it, so an ACCEPT here is terminal for that chain and the
# packet survives to be masqueraded.
#
# This does NOT reopen the LAN: the agentvm chain above runs at priority -10,
# ahead of Docker's filter table, and its drop is terminal.
if command -v iptables >/dev/null && iptables -n -L DOCKER-USER >/dev/null 2>&1; then
    echo "==> Docker detected; allowing $BRIDGE through DOCKER-USER"
    for spec in "-i $BRIDGE" "-o $BRIDGE"; do
        # shellcheck disable=SC2086
        if iptables -C DOCKER-USER $spec -j ACCEPT 2>/dev/null; then
            echo "    already present: $spec"
        else
            # shellcheck disable=SC2086
            iptables -I DOCKER-USER 1 $spec -j ACCEPT
            echo "    inserted: $spec"
        fi
    done
else
    echo "==> No DOCKER-USER chain; skipping Docker coexistence rules"
fi

# Reapply on boot, after libvirt has created virbr0.
cat > /etc/systemd/system/nftables-agentvm.service <<'EOF'
[Unit]
Description=Egress policy for the agent VM
After=libvirtd.service docker.service network-online.target
Wants=libvirtd.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/nft -f /etc/nftables-agentvm.conf
# Re-add the Docker coexistence rules: docker.service flushes DOCKER-USER's
# neighbours on restart, and the chain is empty again after a reboot.
ExecStart=/usr/local/sbin/agentvm-docker-allow

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/sbin/agentvm-docker-allow <<'HELPER'
#!/usr/bin/env bash
# Allow the agent VM's bridge through Docker's FORWARD policy. Idempotent.
set -euo pipefail
BRIDGE="${BRIDGE:-virbr0}"
command -v iptables >/dev/null || exit 0
iptables -n -L DOCKER-USER >/dev/null 2>&1 || exit 0
for spec in "-i $BRIDGE" "-o $BRIDGE"; do
    # shellcheck disable=SC2086
    iptables -C DOCKER-USER $spec -j ACCEPT 2>/dev/null \
        || iptables -I DOCKER-USER 1 $spec -j ACCEPT
done
HELPER
chmod 0755 /usr/local/sbin/agentvm-docker-allow

systemctl daemon-reload
systemctl enable --now nftables-agentvm.service

echo "Applied. Current ruleset:"
nft list table inet agentvm

echo
echo "==> Checking that guest traffic can now be forwarded"
if iptables -n -L DOCKER-USER >/dev/null 2>&1; then
    iptables -n -L DOCKER-USER --line-numbers | head -5
fi
echo
echo "Verify from inside the VM (all three must hold):"
echo "  ping -c2 1.1.1.1                                                    # must reply"
echo "  curl -4 -sS -o /dev/null -w '%{http_code}\\n' https://api.anthropic.com/v1/messages   # expect 405"
echo "  curl -sS --max-time 5 http://<another-LAN-host>/                     # expect timeout"
