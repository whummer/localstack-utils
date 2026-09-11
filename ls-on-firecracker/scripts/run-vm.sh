#!/usr/bin/env bash
# Sets up a tap device on the host, writes a Firecracker VM config, and boots
# the microVM in the background. Waits until LocalStack answers its health
# endpoint over the tap network before returning.
set -euo pipefail

: "${WORK_DIR:?run via 'make', not directly}"
: "${TAP_DEV:?}" "${TAP_IP:?}" "${VM_IP:?}" "${VM_MASK:?}" "${VCPUS:?}" "${MEM_MB:?}"

FC_BIN="$WORK_DIR/bin/firecracker"
KERNEL="$WORK_DIR/images/vmlinux.bin"
ROOTFS="$WORK_DIR/images/localstack.ext4"
SOCKET="$WORK_DIR/firecracker.sock"
CONFIG="$WORK_DIR/vm-config.json"
LOG="$WORK_DIR/firecracker.log"
PIDFILE="$WORK_DIR/firecracker.pid"

if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "[run] a microVM is already running (pid $(cat "$PIDFILE")); run 'make down' first"
  exit 0
fi
rm -f "$SOCKET"

echo "[run] configuring tap device $TAP_DEV ($TAP_IP <-> $VM_IP)"
if ! ip link show "$TAP_DEV" &>/dev/null; then
  sudo ip tuntap add dev "$TAP_DEV" mode tap
fi
sudo ip addr flush dev "$TAP_DEV"
sudo ip addr add "${TAP_IP}/24" dev "$TAP_DEV"
sudo ip link set dev "$TAP_DEV" up
sudo sysctl -qw "net.ipv4.conf.${TAP_DEV}.proxy_arp=1"
sudo sysctl -qw "net.ipv6.conf.${TAP_DEV}.disable_ipv6=1"

# NAT the guest out through the host's default interface. The guest needs
# real internet access for `docker pull` of the Lambda runtime image -- it's
# not just host<->guest traffic like the plain S3-only version of this demo.
NET_BASE="$(echo "$TAP_IP" | awk -F. '{print $1"."$2"."$3".0"}')/24"
HOST_IFACE="$(ip route show default 2>/dev/null | awk '/default/ {print $5; exit}')"
if [[ -n "$HOST_IFACE" ]]; then
  echo "[run] enabling NAT via $HOST_IFACE so the guest can reach the internet"
  sudo sysctl -qw net.ipv4.ip_forward=1
  sudo iptables -t nat -C POSTROUTING -s "$NET_BASE" -o "$HOST_IFACE" -j MASQUERADE 2>/dev/null \
    || sudo iptables -t nat -A POSTROUTING -s "$NET_BASE" -o "$HOST_IFACE" -j MASQUERADE
  sudo iptables -C FORWARD -i "$TAP_DEV" -o "$HOST_IFACE" -j ACCEPT 2>/dev/null \
    || sudo iptables -A FORWARD -i "$TAP_DEV" -o "$HOST_IFACE" -j ACCEPT
  sudo iptables -C FORWARD -i "$HOST_IFACE" -o "$TAP_DEV" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null \
    || sudo iptables -A FORWARD -i "$HOST_IFACE" -o "$TAP_DEV" -m state --state RELATED,ESTABLISHED -j ACCEPT
else
  echo "[run] warning: no default route found on host; guest will have no internet access" >&2
fi

# Any KEY=VALUE on the kernel command line that systemd doesn't otherwise
# recognize is ignored unless it's spelled systemd.setenv=KEY=VALUE, in which
# case it becomes a manager-wide environment variable that every unit
# (including localstack.service) inherits. This is how LOCALSTACK_AUTH_TOKEN
# gets from the CI secret into the guest without baking it into the image.
AUTH_ENV=""
if [[ -n "${LOCALSTACK_AUTH_TOKEN:-}" ]]; then
  AUTH_ENV=" systemd.setenv=LOCALSTACK_AUTH_TOKEN=${LOCALSTACK_AUTH_TOKEN}"
fi

BOOT_ARGS="console=ttyS0 reboot=k panic=1 pci=off ip=${VM_IP}::${TAP_IP}:${VM_MASK}::eth0:off${AUTH_ENV}"

cat > "$CONFIG" <<JSON
{
  "boot-source": {
    "kernel_image_path": "$(realpath "$KERNEL")",
    "boot_args": "${BOOT_ARGS}"
  },
  "drives": [
    {
      "drive_id": "rootfs",
      "path_on_host": "$(realpath "$ROOTFS")",
      "is_root_device": true,
      "is_read_only": false
    }
  ],
  "network-interfaces": [
    {
      "iface_id": "eth0",
      "guest_mac": "AA:FC:00:00:00:01",
      "host_dev_name": "${TAP_DEV}"
    }
  ],
  "machine-config": {
    "vcpu_count": ${VCPUS},
    "mem_size_mib": ${MEM_MB}
  }
}
JSON

echo "[run] booting microVM (log: $LOG)"
sudo setsid "$FC_BIN" --api-sock "$SOCKET" --config-file "$CONFIG" \
  > "$LOG" 2>&1 < /dev/null &
disown
echo $! | sudo tee "$PIDFILE" >/dev/null

SSH_KEY="$WORK_DIR/images/id_rsa"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR
          -o BatchMode=yes -o ConnectTimeout=5 -i "$SSH_KEY" "root@${VM_IP}")

# A cold `lstk` pull of the LocalStack image can legitimately take minutes,
# so the overall budget below stays generous -- but a broken docker.service
# or localstack.service (lstk itself failing to start, no auto-restart)
# reaches a terminal "failed" state within seconds of boot, so there is no
# reason to wait out the full budget for that case. Poll for it (after a
# short boot grace period) and bail immediately once either has failed.
is_service_broken() {
  [[ -f "$SSH_KEY" ]] || return 1
  local states
  states=$(ssh "${SSH_OPTS[@]}" 'systemctl is-active docker.service localstack.service' 2>/dev/null || echo "")
  [[ "$states" == *failed* ]]
}

echo "[run] waiting for LocalStack to become healthy at http://${VM_IP}:4566 ..."
for i in $(seq 1 90); do
  if curl -fsS "http://${VM_IP}:4566/_localstack/health" >/dev/null 2>&1; then
    echo "[run] LocalStack is up: http://${VM_IP}:4566"
    exit 0
  fi
  if (( i > 12 )) && (( i % 6 == 0 )) && is_service_broken; then
    echo "[run] docker.service or localstack.service failed inside the guest; not waiting further. Run 'make diagnose' for details." >&2
    exit 1
  fi
  sleep 5
done

echo "[run] timed out waiting for LocalStack; check $LOG or 'make diagnose'" >&2
exit 1
