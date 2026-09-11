#!/usr/bin/env bash
# Downloads the three things Firecracker needs to boot a microVM:
#   1. the firecracker binary itself
#   2. an uncompressed guest kernel (vmlinux)
#   3. a base guest rootfs (ext4 image)
#
# Kernel/rootfs are pulled from Firecracker's public CI bucket, which is the
# same source used in the project's own getting-started guide. We resolve
# "latest for this CI track" dynamically so the URLs don't go stale.
set -euo pipefail

: "${WORK_DIR:?run via 'make', not directly}"
: "${FC_VERSION:?}"
: "${CI_TRACK:?}"
: "${ARCH:?}"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "[download] error: Firecracker requires Linux + KVM (/dev/kvm)." >&2
  echo "           This host is $(uname -s), so it cannot run this demo." >&2
  echo "           Try a Linux box with KVM, an EC2 .metal instance, or a" >&2
  echo "           GitHub Actions ubuntu-latest runner instead." >&2
  exit 1
fi

if [[ ! -e /dev/kvm ]]; then
  echo "[download] error: /dev/kvm not found. Firecracker needs KVM, which" >&2
  echo "           usually means bare metal or a host with nested" >&2
  echo "           virtualization enabled (not a typical cloud VM)." >&2
  exit 1
fi

BIN_DIR="$WORK_DIR/bin"
IMG_DIR="$WORK_DIR/images"
mkdir -p "$BIN_DIR" "$IMG_DIR"

if [[ -x "$BIN_DIR/firecracker" ]]; then
  echo "[download] firecracker binary already present, skipping"
else
  echo "[download] fetching firecracker $FC_VERSION for $ARCH"
  tmp=$(mktemp -d)
  curl -fsSL "https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-${ARCH}.tgz" \
    | tar -xz -C "$tmp"
  find "$tmp" -type f -name "firecracker-${FC_VERSION}-${ARCH}" -exec cp {} "$BIN_DIR/firecracker" \;
  chmod +x "$BIN_DIR/firecracker"
  rm -rf "$tmp"
fi

# lstk is LocalStack's own CLI: it pulls the LocalStack image, starts it as a
# container against the guest's Docker daemon, and waits for it to be ready.
# It ends up baked into the guest rootfs (see build-rootfs.sh), not run on
# the host, so we resolve its release for the *guest's* architecture.
GOARCH="$(if [[ "$ARCH" == "aarch64" ]]; then echo arm64; else echo amd64; fi)"
if [[ -x "$BIN_DIR/lstk" ]]; then
  echo "[download] lstk binary already present, skipping"
else
  echo "[download] fetching latest lstk for linux/$GOARCH"
  lstk_tag=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/localstack/lstk/releases/latest" | sed 's#.*/##')
  [[ -n "$lstk_tag" ]] || { echo "could not resolve the latest lstk release" >&2; exit 1; }
  lstk_ver="${lstk_tag#v}"
  tmp=$(mktemp -d)
  curl -fsSL "https://github.com/localstack/lstk/releases/download/${lstk_tag}/lstk_${lstk_ver}_linux_${GOARCH}.tar.gz" \
    | tar -xz -C "$tmp" lstk
  mv "$tmp/lstk" "$BIN_DIR/lstk"
  chmod +x "$BIN_DIR/lstk"
  rm -rf "$tmp"
fi

if [[ -f "$IMG_DIR/vmlinux.bin" ]]; then
  echo "[download] kernel already present, skipping"
else
  echo "[download] resolving latest CI kernel for track $CI_TRACK/$ARCH"
  kernel_key=$(curl -fsSL "http://spec.ccfc.min.s3.amazonaws.com/?prefix=firecracker-ci/${CI_TRACK}/${ARCH}/vmlinux-&list-type=2" \
    | grep -oP '(?<=<Key>)[^<]+' \
    | grep -E "^firecracker-ci/${CI_TRACK}/${ARCH}/vmlinux-[0-9]+\.[0-9]+\.[0-9]+$" \
    | sort -V | tail -1)
  [[ -n "$kernel_key" ]] || { echo "could not resolve a kernel for CI track $CI_TRACK/$ARCH" >&2; exit 1; }
  curl -fsSL "https://s3.amazonaws.com/spec.ccfc.min/${kernel_key}" -o "$IMG_DIR/vmlinux.bin"
fi

if [[ -f "$IMG_DIR/base.ext4" ]]; then
  echo "[download] base rootfs already present, skipping"
else
  echo "[download] resolving latest CI rootfs for track $CI_TRACK/$ARCH"
  rootfs_key=$(curl -fsSL "http://spec.ccfc.min.s3.amazonaws.com/?prefix=firecracker-ci/${CI_TRACK}/${ARCH}/ubuntu-&list-type=2" \
    | grep -oP '(?<=<Key>)[^<]+' \
    | grep -E "^firecracker-ci/${CI_TRACK}/${ARCH}/ubuntu-[0-9]+\.[0-9]+\.ext4$" \
    | sort -V | tail -1)
  [[ -n "$rootfs_key" ]] || { echo "could not resolve a base rootfs for CI track $CI_TRACK/$ARCH" >&2; exit 1; }
  curl -fsSL "https://s3.amazonaws.com/spec.ccfc.min/${rootfs_key}" -o "$IMG_DIR/base.ext4"

  # This base image ships sshd running with a pre-authorized root key; the
  # matching private key is published as a sibling of the .ext4 file (e.g.
  # ubuntu-22.04.id_rsa next to ubuntu-22.04.ext4), not an appended suffix.
  # Handy for `make ssh` and pulling diagnostics when a boot fails.
  rsa_key="${rootfs_key%.ext4}.id_rsa"
  curl -fsSL "https://s3.amazonaws.com/spec.ccfc.min/${rsa_key}" -o "$IMG_DIR/id_rsa" \
    && chmod 600 "$IMG_DIR/id_rsa" \
    || echo "[download] warning: no matching SSH key found for this rootfs, 'make ssh' won't work" >&2
fi

echo "[download] done -> $BIN_DIR/firecracker, $BIN_DIR/lstk, $IMG_DIR/vmlinux.bin, $IMG_DIR/base.ext4"
