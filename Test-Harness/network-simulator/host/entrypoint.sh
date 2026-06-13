#!/bin/sh
# Simulated LAN host: emits the kind of chatter the gateway's autonet sniffer
# learns from — fresh ARP every cycle, unicast pings to the gateway and peers,
# and a directed broadcast (which feeds netmask inference).
#
# POSIX sh (Alpine ash): no $RANDOM, so jitter comes from /dev/urandom.
set -u

: "${SELF_NAME:=sim-host}"
: "${GATEWAY:=192.168.77.1}"
: "${BROADCAST:=192.168.77.255}"
: "${PEERS:=}"

self_ip="$(ip -4 -o addr show eth0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)"
echo "sim[${SELF_NAME}]: ip=${self_ip:-?} gw=${GATEWAY} bcast=${BROADCAST} peers=[${PEERS}]"

while true; do
    # Flushing the neighbor table forces a fresh ARP who-has on the next ping,
    # guaranteeing steady ARP traffic for the sniffer to observe.
    ip -s neigh flush all >/dev/null 2>&1 || true

    ping -c1 -W1 "${GATEWAY}" >/dev/null 2>&1 || true
    for p in ${PEERS}; do
        ping -c1 -W1 "${p}" >/dev/null 2>&1 || true
    done

    # Directed broadcast -> dst MAC ff:ff:ff:ff:ff:ff, dst IP x.x.x.255.
    # This is the signal autonet uses to derive the prefix/netmask.
    ping -b -c1 -W1 "${BROADCAST}" >/dev/null 2>&1 || true

    # 3-7s jitter so the timing looks organic rather than a metronome.
    sleep $(( $(od -An -N1 -tu1 /dev/urandom) % 5 + 3 ))
done
