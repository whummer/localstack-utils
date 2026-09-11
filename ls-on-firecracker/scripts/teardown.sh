#!/usr/bin/env bash
# Stops the microVM and removes the tap device. Safe to run even if nothing
# is up.
set -euo pipefail

: "${WORK_DIR:?run via 'make', not directly}"
: "${TAP_DEV:?}"
: "${TAP_IP:?}"

PIDFILE="$WORK_DIR/firecracker.pid"

if [[ -f "$PIDFILE" ]]; then
  pid=$(cat "$PIDFILE")
  if kill -0 "$pid" 2>/dev/null; then
    echo "[down] stopping microVM (pid $pid)"
    sudo kill "$pid" 2>/dev/null || true
    sleep 1
    sudo kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
fi
rm -f "$WORK_DIR/firecracker.sock"

NET_BASE="$(echo "$TAP_IP" | awk -F. '{print $1"."$2"."$3".0"}')/24"
HOST_IFACE="$(ip route show default 2>/dev/null | awk '/default/ {print $5; exit}')"
if [[ -n "$HOST_IFACE" ]]; then
  echo "[down] removing NAT rules for $NET_BASE via $HOST_IFACE"
  sudo iptables -t nat -D POSTROUTING -s "$NET_BASE" -o "$HOST_IFACE" -j MASQUERADE 2>/dev/null || true
  sudo iptables -D FORWARD -i "$TAP_DEV" -o "$HOST_IFACE" -j ACCEPT 2>/dev/null || true
  sudo iptables -D FORWARD -i "$HOST_IFACE" -o "$TAP_DEV" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
fi

if ip link show "$TAP_DEV" &>/dev/null; then
  echo "[down] removing tap device $TAP_DEV"
  sudo ip link del "$TAP_DEV" 2>/dev/null || true
fi

echo "[down] done"
