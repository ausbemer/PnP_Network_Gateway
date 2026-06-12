#!/usr/bin/env bash
# tailscale-gateway-watch.sh
# Hot-swap support: watches for network changes (e.g. moving the Pi to a
# different LAN) and restarts the gateway when the detected subnet no longer
# matches what the Tailscale container is advertising — so the device picks up
# a new network without a power cycle.
#
# Mechanism: `ip monitor` blocks and emits a line on every address/route/link
# change, straight from the kernel — so this works regardless of whether the
# base OS uses NetworkManager, dhcpcd, or systemd-networkd. Events are debounced
# (we wait for the network to go quiet, which also lets DHCP finish), then the
# current subnet is compared against the container's advertised route. A restart
# happens ONLY when they differ, which prevents restart loops caused by the
# gateway's own tailscale0 interface coming up.
#
# Run by tailscale-gateway-watch.service.

set -uo pipefail

CONTAINER_NAME="tailscale-gateway"
DEBOUNCE=5   # seconds of network quiet to wait for before reconciling

# ── Detect the LAN subnet (same logic as start-tailscale-gateway.sh) ──────────
detect_subnet() {
    local iface subnet
    iface=$(ip route show default 2>/dev/null | awk '/default/ {print $5; exit}')
    [[ -z "${iface}" ]] && return 1
    subnet=$(ip route show dev "${iface}" proto kernel 2>/dev/null \
        | awk '{print $1; exit}')
    [[ -z "${subnet}" ]] && return 1
    printf '%s' "${subnet}"
}

# ── What is the running container currently advertising? ───────────────────────
advertised_subnet() {
    docker inspect "${CONTAINER_NAME}" \
        --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
        | sed -n 's/^TS_ROUTES=//p' | head -1
}

# ── Restart the gateway if (and only if) the subnet changed ───────────────────
AUTONET="/usr/local/bin/tailscale-gateway-autonet.sh"

reconcile() {
    local detected current
    if ! detected=$(detect_subnet); then
        # No default route. We may have just been plugged into a network with no
        # DHCP — try the auto-static fallback (short DHCP wait since this is a
        # live hot-swap, not a cold boot). If it establishes a route, the address
        # it adds will fire another event and we'll reconcile again.
        if [[ -x "${AUTONET}" ]]; then
            echo "watch: no default route — attempting auto-network configuration"
            DHCP_WAIT=5 "${AUTONET}" || true
        else
            echo "watch: no usable default route yet — skipping"
        fi
        return 0
    fi
    current=$(advertised_subnet)
    if [[ "${detected}" != "${current}" ]]; then
        echo "watch: subnet changed (advertised='${current:-none}' detected='${detected}') — restarting gateway"
        systemctl restart "${CONTAINER_NAME}.service"
    else
        echo "watch: subnet unchanged (${detected}) — no action"
    fi
}

echo "watch: starting; monitoring address/route/link changes..."

# Sync once at startup in case the network differs from what's advertised.
reconcile

# Main loop: block on the first event, drain the burst until the network is
# quiet for DEBOUNCE seconds, then reconcile exactly once.
ip monitor address route link 2>/dev/null | while read -r _; do
    while read -r -t "${DEBOUNCE}" _; do :; done   # drain burst / settle DHCP
    reconcile
done

# If we get here, `ip monitor` exited; let systemd restart us.
echo "watch: ip monitor exited; restarting service"
exit 1
