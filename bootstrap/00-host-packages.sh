#!/usr/bin/env bash
# Privileged host bootstrap for the Slack-controlled Claude Code daemon.
#
# Run this yourself:   sudo ./bootstrap/00-host-packages.sh
#
# Everything here needs root. After it completes, the `tibber` user can drive
# libvirt directly and the daemon's --user unit survives reboot, so the rest of
# the provisioning runs unprivileged.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "must run as root: sudo $0" >&2
    exit 1
fi

TARGET_USER="${TARGET_USER:-tibber}"

echo "==> Installing packages"
# qemu-base pulls qemu-img, qemu-common, qemu-system-x86 and virtiofsd.
# dnsmasq is what libvirt's `default` NAT network needs.
# edk2-ovmf provides UEFI firmware; cloud-image-utils provides cloud-localds.
pacman -S --needed --noconfirm \
    qemu-base \
    edk2-ovmf \
    dnsmasq \
    cloud-image-utils \
    libvirt

echo "==> Enabling libvirt"
systemctl enable --now libvirtd.socket
# Give the socket a moment to spawn libvirtd on first use.
virsh --connect qemu:///system version >/dev/null

echo "==> Starting the default NAT network"
if ! virsh --connect qemu:///system net-info default >/dev/null 2>&1; then
    virsh --connect qemu:///system net-define /usr/share/libvirt/networks/default.xml
fi
virsh --connect qemu:///system net-start default 2>/dev/null || true
virsh --connect qemu:///system net-autostart default

echo "==> Preparing VM storage"
# 10-provision-vm.sh runs unprivileged and writes the base image, seed ISO and
# qcow2 here, so the directory has to be group-writable by libvirt members.
# Ships as root:root 0755, which makes provisioning fail with EACCES.
IMAGES_DIR="${IMAGES_DIR:-/var/lib/libvirt/images}"

# Put it on its own ZFS dataset when we can: that is what makes
# `zfs snapshot ssd/vm@<tag>` a usable rollback for the VM disk, and it keeps the
# VM's churn out of the root dataset's snapshots.
ZFS_POOL="${ZFS_POOL:-ssd}"
ZFS_DATASET="$ZFS_POOL/vm"
if command -v zfs >/dev/null && zpool list "$ZFS_POOL" >/dev/null 2>&1; then
    if zfs list "$ZFS_DATASET" >/dev/null 2>&1; then
        echo "    dataset $ZFS_DATASET already exists"
    elif [[ -n "$(ls -A "$IMAGES_DIR" 2>/dev/null)" ]]; then
        # Never mount over existing images — that would hide them.
        echo "    $IMAGES_DIR is not empty; leaving it on the root dataset"
    else
        echo "    creating $ZFS_DATASET at $IMAGES_DIR"
        # 64k recordsize suits qcow2 far better than the 128k default.
        zfs create -o "mountpoint=$IMAGES_DIR" -o recordsize=64k "$ZFS_DATASET"
    fi
else
    echo "    no '$ZFS_POOL' zpool; using $IMAGES_DIR as-is"
fi

mkdir -p "$IMAGES_DIR"
chgrp libvirt "$IMAGES_DIR"
# setgid so files land group-libvirt and stay writable by the group.
chmod 2775 "$IMAGES_DIR"

echo "==> Granting $TARGET_USER libvirt access"
usermod -aG libvirt,kvm "$TARGET_USER"

echo "==> Enabling systemd lingering for $TARGET_USER"
# Without this, `systemctl --user` units do not start at boot and are killed
# on logout — the daemon would not survive a reboot.
loginctl enable-linger "$TARGET_USER"

echo
echo "Done. Verify as $TARGET_USER (after a fresh login, or 'newgrp libvirt'):"
echo "  virsh list --all"
echo "  virsh net-list"
echo "  loginctl show-user $TARGET_USER -p Linger   # expect Linger=yes"
