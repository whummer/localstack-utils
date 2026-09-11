#!/usr/bin/env bash
# Turns the pristine CI rootfs into a "LocalStack appliance": grows the ext4
# image, chroots into it, pip-installs LocalStack + awslocal, and registers a
# systemd unit that starts LocalStack on boot.
#
# The chroot shares the host's network namespace (it's just a mounted
# directory, not a container), so apt/pip work normally as long as the host
# has internet access. The guest VM itself does NOT get internet access at
# boot -- it doesn't need it, everything is already baked into the image.
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

# This CI-provided base image ships apt/dpkg binaries but an empty
# /var/lib/dpkg (no `status` file, no info/updates/triggers dirs) -- it was
# stripped for Firecracker's own network-test use, not general package
# installs. Bootstrap a fresh, empty dpkg database, the same thing tools
# like debootstrap do, so apt has something to work from.
sudo mkdir -p "$MNT/var/lib/dpkg/info" "$MNT/var/lib/dpkg/updates" "$MNT/var/lib/dpkg/triggers"
sudo touch "$MNT/var/lib/dpkg/status" "$MNT/var/lib/dpkg/available"

echo "[rootfs] installing Docker + LocalStack inside the guest image (this can take a few minutes)"
sudo chroot "$MNT" /bin/bash -c '
  set -euo pipefail
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3-pip python3-venv docker.io >/dev/null
  pip3 install --break-system-packages -q localstack awscli-local
  systemctl enable docker.service
'

# LocalStack uses the guest's own Docker daemon to run the Lambda executor
# container, exactly like real Lambda uses a container runtime inside its
# Firecracker microVM. It needs to start after (and depend on) Docker.
sudo tee "$MNT/etc/systemd/system/localstack.service" >/dev/null <<'UNIT'
[Unit]
Description=LocalStack
After=docker.service network.target
Requires=docker.service

[Service]
Environment=LOCALSTACK_HOST=0.0.0.0
ExecStart=/usr/local/bin/localstack start --host
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT

sudo chroot "$MNT" systemctl enable localstack.service

# The chroot borrowed the host's /etc/resolv.conf (often a systemd-resolved
# stub at 127.0.0.53) to resolve apt/pip mirrors during the build above. That
# address is meaningless once the image boots as its own VM, so pin a public
# resolver for runtime -- the guest needs it to pull the Lambda runtime image
# from ECR public over the NAT'd link `run-vm.sh` sets up.
sudo tee "$MNT/etc/resolv.conf" >/dev/null <<'EOF'
nameserver 8.8.8.8
nameserver 1.1.1.1
EOF

echo "[rootfs] built -> $OUT_IMG"
