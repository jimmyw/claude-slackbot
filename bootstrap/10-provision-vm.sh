#!/usr/bin/env bash
# Provision the agent VM. Run as tibber AFTER 00-host-packages.sh and a fresh
# login (the libvirt group must be active — check with `id`).
#
#   ./bootstrap/10-provision-vm.sh
#
# Idempotent-ish: refuses to clobber an existing domain. To rebuild, run
# `virsh destroy agent-vm; virsh undefine agent-vm --nvram` first.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# For an unprivileged user `virsh uri` resolves to qemu:///session — a separate,
# empty hypervisor instance. Everything here belongs to the system instance, so
# pin it for virsh and virt-install alike.
export LIBVIRT_DEFAULT_URI="${LIBVIRT_DEFAULT_URI:-qemu:///system}"
DOMAIN="${DOMAIN:-agent-vm}"
VM_DIR="${VM_DIR:-/var/lib/libvirt/images}"
DISK_GB="${DISK_GB:-40}"
RAM_MB="${RAM_MB:-4096}"
VCPUS="${VCPUS:-4}"
KEY="${KEY:-$HOME/.ssh/agent_vm_ed25519}"
ADMIN_KEY="${ADMIN_KEY:-$HOME/.ssh/agent_vm_admin_ed25519}"
IMAGE_URL="${IMAGE_URL:-https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2}"

if ! id -nG | tr ' ' '\n' | grep -qx libvirt; then
    echo "ERROR: the libvirt group is not active in this shell." >&2
    echo "Log out and back in (or run 'newgrp libvirt') and retry." >&2
    exit 1
fi

if [[ ! -w "$VM_DIR" ]]; then
    echo "ERROR: $VM_DIR is not writable by $(id -un)." >&2
    echo "Re-run the host bootstrap (it is idempotent and fixes this):" >&2
    echo "  sudo ./bootstrap/00-host-packages.sh" >&2
    exit 1
fi

if virsh domuuid "$DOMAIN" >/dev/null 2>&1; then
    echo "ERROR: domain '$DOMAIN' already exists. Remove it first:" >&2
    echo "  virsh destroy $DOMAIN; virsh undefine $DOMAIN --nvram" >&2
    exit 1
fi

echo "==> Generating SSH keys"
# Two keys, deliberately: the daemon key is pinned to a forced command and
# cannot get a shell, so a second admin key is the only way to reach the guest
# for `claude setup-token` and for pushing updated vm-files later.
for k in "$KEY" "$ADMIN_KEY"; do
    if [[ ! -f "$k" ]]; then
        ssh-keygen -t ed25519 -N '' -C "$(basename "$k")" -f "$k"
    else
        echo "    reusing $k"
    fi
done

echo "==> Building the cloud-init seed"
# The guest files are embedded in the seed rather than pushed afterwards: on a
# fresh VM there is nothing to push with, since the daemon key has no shell.
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
python3 "$REPO_DIR/bootstrap/render-cloud-init.py" \
    "$REPO_DIR" "$KEY.pub" "$ADMIN_KEY.pub" > "$WORK/user-data"
printf 'instance-id: %s\nlocal-hostname: %s\n' "$DOMAIN-$(date +%s)" "$DOMAIN" \
    > "$WORK/meta-data"
cloud-localds "$WORK/seed.iso" "$WORK/user-data" "$WORK/meta-data"

echo "==> Fetching the base image"
BASE="$VM_DIR/debian-13-genericcloud-amd64.qcow2"
if [[ ! -f "$BASE" ]]; then
    curl -fL --progress-bar -o "$BASE" "$IMAGE_URL"
fi

echo "==> Creating the VM disk (${DISK_GB}G)"
install -m 0600 "$WORK/seed.iso" "$VM_DIR/$DOMAIN-seed.iso"
qemu-img create -f qcow2 -F qcow2 -b "$BASE" "$VM_DIR/$DOMAIN.qcow2" "${DISK_GB}G"

echo "==> Defining and starting the domain"
virt-install \
    --connect "$LIBVIRT_DEFAULT_URI" \
    --name "$DOMAIN" \
    --memory "$RAM_MB" \
    --vcpus "$VCPUS" \
    --cpu host-passthrough \
    --boot uefi \
    --disk "path=$VM_DIR/$DOMAIN.qcow2,format=qcow2,bus=virtio" \
    --disk "path=$VM_DIR/$DOMAIN-seed.iso,device=cdrom" \
    --network network=default,model=virtio \
    --graphics none \
    --console pty,target_type=serial \
    --osinfo detect=on,name=debian12 \
    --import \
    --noautoconsole

virsh autostart "$DOMAIN"

echo "==> Waiting for a DHCP lease (cloud-init also needs a few minutes)"
IP=""
for _ in $(seq 1 60); do
    IP="$(virsh -q domifaddr "$DOMAIN" 2>/dev/null \
        | awk '/ipv4/ {split($4, a, "/"); print a[1]; exit}')"
    [[ -n "$IP" ]] && break
    sleep 5
done

if [[ -z "$IP" ]]; then
    echo "No lease yet. Watch progress with: virsh console $DOMAIN" >&2
    exit 1
fi

echo
echo "VM '$DOMAIN' is up at $IP"
echo
echo "Next:"
echo "  1. sudo ./bootstrap/20-nftables-egress.sh          # lock down egress"
echo "  2. ./bootstrap/30-install-vm-files.sh $IP          # install hook + CLAUDE.md"
echo "  3. Put this in daemon/.env:  VM_HOST=$IP"
echo "     and                       VM_SSH_KEY=$KEY"
