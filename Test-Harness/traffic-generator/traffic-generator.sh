#!/usr/bin/env bash
# traffic-generator.sh
# Generates the LAN chatter that `tailscale-gateway-autonet` needs to infer a
# DHCP-less segment. Run it on any Linux box (e.g. a Jetson) sitting on the test
# segment alongside the Pi-under-test. No Docker required.
#
# It optionally claims one or more static IPs on the interface, then loops:
#   - forces a fresh ARP for the gateway each cycle  (subnet + gateway signal)
#   - sends a directed broadcast                      (netmask signal)
# which are exactly what autonet keys on. Extra IPs show up as extra hosts to the
# dashboard's arp-scan (they share this box's MAC — fine for testing).
#
# Usage:
#   sudo ./traffic-generator.sh
#
# Configure via environment (or edit the defaults):
#   IFACE      interface on the test segment   (default: eth0)
#   GATEWAY    test-segment gateway            (default: 192.168.77.1)
#   PREFIX     subnet prefix length            (default: 24)
#   IPS        static IPs to claim (space-sep) (default: .61 .62 .63)
#   BROADCAST  directed broadcast address      (default: 192.168.77.255)
#   INTERVAL   seconds between cycles          (default: 4)

set -u

IFACE="${IFACE:-eth0}"
GATEWAY="${GATEWAY:-192.168.77.1}"
PREFIX="${PREFIX:-24}"
IPS="${IPS:-192.168.77.61 192.168.77.62 192.168.77.63}"
BROADCAST="${BROADCAST:-192.168.77.255}"
INTERVAL="${INTERVAL:-4}"

if [ "$(id -u)" -ne 0 ]; then
    echo "traffic-gen: must run as root (sudo)" >&2
    exit 1
fi

echo "traffic-gen: iface=${IFACE} gw=${GATEWAY} ips=[${IPS}] bcast=${BROADCAST}"

# Claim the static IPs (idempotent). First is primary; the rest are aliases that
# arp-scan will still discover.
for ip in ${IPS}; do
    if ip addr add "${ip}/${PREFIX}" dev "${IFACE}" 2>/dev/null; then
        echo "traffic-gen: added ${ip}/${PREFIX}"
    else
        echo "traffic-gen: ${ip}/${PREFIX} already present (or failed — check IFACE/root)"
    fi
done
ip link set "${IFACE}" up 2>/dev/null || true
ip route replace default via "${GATEWAY}" dev "${IFACE}" 2>/dev/null || true

# Remove the IPs we added when stopped, so the box is left clean.
cleanup() {
    echo
    echo "traffic-gen: cleaning up assigned IPs..."
    for ip in ${IPS}; do
        ip addr del "${ip}/${PREFIX}" dev "${IFACE}" 2>/dev/null || true
    done
    exit 0
}
trap cleanup INT TERM

echo "traffic-gen: generating chatter on ${IFACE} (Ctrl-C to stop)..."
while true; do
    ip -s neigh flush all >/dev/null 2>&1 || true   # force a fresh ARP who-has
    ping -c1 -W1 "${GATEWAY}"   >/dev/null 2>&1 || true
    ping -b -c1 -W1 "${BROADCAST}" >/dev/null 2>&1 || true
    sleep "${INTERVAL}"
done
