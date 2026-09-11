#!/usr/bin/env bash
# Turns the pristine CI rootfs into a "LocalStack appliance": grows the ext4
# image, chroots into it to install Docker and drop in the lstk binary, and
# registers a systemd unit that runs `lstk start` (which pulls and runs the
# LocalStack container against the guest's own Docker daemon) on boot.
#
# The chroot shares the host's network namespace (it's just a mounted
# directory, not a container), so apt-get works normally here as long as the
# host has internet access. The guest VM itself needs its own internet
# access too, at boot time this time -- lstk has to pull the LocalStack
# image and Lambda invocations pull runtime images -- which is what the NAT
# setup in run-vm.sh is for.
set -euo pipefail

: "${WORK_DIR:?run via 'make', not directly}"

IMG_DIR="$WORK_DIR/images"
BASE_IMG="$IMG_DIR/base.ext4"
OUT_IMG="$IMG_DIR/localstack.ext4"
MNT="$WORK_DIR/rootfs-mnt"

if [[ -f "$OUT_IMG" ]]; then
  echo "[rootfs] $OUT_IMG already built, skipping (delete it, or 'make clean', to rebuild)"
  exit 0
fi

echo "[rootfs] cloning base image and growing it to make room for LocalStack + Docker"
cp "$BASE_IMG" "$OUT_IMG"
truncate -s 8G "$OUT_IMG"
e2fsck -fy "$OUT_IMG" || true
resize2fs "$OUT_IMG"

mkdir -p "$MNT"
LOOP_DEV=$(sudo losetup --find --show "$OUT_IMG")
cleanup() {
  sudo umount -R "$MNT" 2>/dev/null || true
  sudo losetup -d "$LOOP_DEV" 2>/dev/null || true
}
trap cleanup EXIT

sudo mount "$LOOP_DEV" "$MNT"
sudo cp /etc/resolv.conf "$MNT/etc/resolv.conf"
sudo mount --bind /dev "$MNT/dev"
sudo mount --bind /proc "$MNT/proc"
sudo mount --bind /sys "$MNT/sys"
# The base image's /tmp and /run are normally populated by systemd-tmpfiles
# at boot; without that, apt/dpkg (which write scratch files there) fail
# with confusing "No such file or directory" errors inside the chroot.
sudo mkdir -p "$MNT/tmp" "$MNT/run"
sudo mount -t tmpfs tmpfs "$MNT/tmp"
sudo mount -t tmpfs tmpfs "$MNT/run"
sudo chmod 1777 "$MNT/tmp"

# This CI-provided base image ships apt/dpkg binaries, but it was stripped
# for Firecracker's own network-test use, not general package installs:
# /var/cache/apt, /var/lib/apt and /var/log don't exist at all, and
# /var/lib/dpkg is an empty directory with no status file. Recreate the
# standard skeleton apt/dpkg expect -- the same bootstrap debootstrap itself
# does for a fresh root -- before touching either.
sudo mkdir -p \
  "$MNT/var/cache/apt/archives/partial" \
  "$MNT/var/lib/apt/lists/partial" \
  "$MNT/var/log/apt" \
  "$MNT/var/lib/dpkg/info" \
  "$MNT/var/lib/dpkg/updates" \
  "$MNT/var/lib/dpkg/triggers" \
  "$MNT/var/backups"
sudo touch "$MNT/var/lib/dpkg/status" "$MNT/var/lib/dpkg/available"

echo "[rootfs] installing Docker + LocalStack inside the guest image (this can take a few minutes)"
sudo chroot "$MNT" /bin/bash -c '
  set -euo pipefail
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  # --force-confdef/--force-confold: some packages (libpam-modules and
  # friends) think their conffiles were locally modified in this base image
  # and prompt for a merge decision on stdin, which is not a TTY here.
  apt-get install -y -qq \
    -o Dpkg::Options::=--force-confdef \
    -o Dpkg::Options::=--force-confold \
    docker.io >/dev/null
  # The Firecracker CI kernel does not compile in nf_tables (only the
  # legacy x_tables framework), but Ubuntu 22.04 iptables defaults to the
  # nftables backend -- dockerd then fails at startup with "Failed to
  # initialize nft: Protocol not supported". Switch to the legacy backend,
  # which the kernel does support.
  update-alternatives --set iptables /usr/sbin/iptables-legacy
  update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy
  systemctl enable docker.service
'
sudo cp "$WORK_DIR/bin/lstk" "$MNT/usr/local/bin/lstk"
sudo chmod +x "$MNT/usr/local/bin/lstk"

# lstk pulls the LocalStack image and starts it as a container against the
# guest's own Docker daemon -- the container is what actually runs LocalStack
# and spawns Lambda executor containers, exactly like real Lambda uses a
# container runtime inside its Firecracker microVM. `lstk start` blocks
# until the emulator is ready and then exits, so the unit that "is" this
# service is really the container, not this process -- hence oneshot +
# RemainAfterExit rather than a long-running ExecStart.
sudo tee "$MNT/etc/systemd/system/localstack.service" >/dev/null <<'UNIT'
[Unit]
Description=LocalStack (via lstk)
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
# systemd services don't get $HOME the way login shells do, but lstk needs
# it to resolve its config/cache directory (~/.cache/lstk/...); this unit
# runs as root (no User= override), so point it at root's home.
Environment=HOME=/root
ExecStart=/usr/local/bin/lstk start --non-interactive --timeout 120s
TimeoutStartSec=200
# No auto-restart: a single clear failure (visible via `systemctl is-failed`
# and its journal) is far more useful for a demo/CI than systemd silently
# retrying a broken command every few seconds for the entire boot budget,
# burying the real error under repeated "Failed to start" lines. Re-run
# `make up` (which boots a fresh VM) to retry.
Restart=no

[Install]
WantedBy=multi-user.target
UNIT

sudo chroot "$MNT" systemctl enable localstack.service

# The chroot borrowed the host's /etc/resolv.conf (often a systemd-resolved
# stub at 127.0.0.53) to resolve apt mirrors during the build above. That
# address is meaningless once the image boots as its own VM, so pin a public
# resolver for runtime -- the guest needs it to pull the LocalStack image
# (lstk) and the Lambda runtime image (LocalStack itself) over the NAT'd
# link run-vm.sh sets up.
sudo tee "$MNT/etc/resolv.conf" >/dev/null <<'EOF'
nameserver 8.8.8.8
nameserver 1.1.1.1
EOF

echo "[rootfs] built -> $OUT_IMG"
