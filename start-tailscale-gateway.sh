#!/usr/bin/env bash
# start-tailscale-gateway.sh
# Detects the DHCP-assigned interface/subnet and starts the Tailscale
# gateway container advertising that route.
#
# Intended to be called by tailscale-gateway.service, which runs only
# after network-online.target (i.e. after DHCP has completed).

set -euo pipefail

CONTAINER_NAME="tailscale-gateway"
AUTHKEY_FILE="/etc/tailscale-gateway/authkey"
STATE_VOLUME="tailscale-gateway-state"

# ── 1. Detect network ─────────────────────────────────────────────────────────

# The interface used for the default route is the one DHCP configured.
IFACE=$(ip route show default 2>/dev/null | awk '/default/ {print $5}' | head -1)

if [[ -z "${IFACE}" ]]; then
    echo "ERROR: No default route found — DHCP may not have completed." >&2
    exit 1
fi

# The directly-connected (proto kernel) route on that interface is the subnet.
SUBNET=$(ip route show dev "${IFACE}" proto kernel 2>/dev/null \
    | awk '{print $1}' | head -1)

if [[ -z "${SUBNET}" ]]; then
    echo "ERROR: Could not determine subnet for interface ${IFACE}." >&2
    exit 1
fi

echo "Interface : ${IFACE}"
echo "Subnet    : ${SUBNET}"

# ── 2. Resolve auth key ───────────────────────────────────────────────────────

if [[ -z "${TS_AUTHKEY:-}" ]]; then
    if [[ -f "${AUTHKEY_FILE}" ]]; then
        TS_AUTHKEY=$(cat "${AUTHKEY_FILE}")
    else
        echo "ERROR: TS_AUTHKEY is not set and ${AUTHKEY_FILE} does not exist." >&2
        echo "       Create the file with your Tailscale auth key, e.g.:" >&2
        echo "         sudo mkdir -p /etc/tailscale-gateway" >&2
        echo "         echo 'tskey-auth-...' | sudo tee /etc/tailscale-gateway/authkey" >&2
        echo "         sudo chmod 600 /etc/tailscale-gateway/authkey" >&2
        exit 1
    fi
fi

# ── 3. Ensure the state volume exists ─────────────────────────────────────────

docker volume inspect "${STATE_VOLUME}" &>/dev/null \
    || docker volume create "${STATE_VOLUME}" >/dev/null

# ── 4. Remove stale container (idempotent restart) ────────────────────────────

if docker inspect "${CONTAINER_NAME}" &>/dev/null; then
    echo "Removing existing container ${CONTAINER_NAME}..."
    docker rm -f "${CONTAINER_NAME}" >/dev/null
fi

# ── 5. Start the Tailscale container ─────────────────────────────────────────

echo "Starting Tailscale gateway, advertising route ${SUBNET}..."

docker run -d \
    --name "${CONTAINER_NAME}" \
    --restart unless-stopped \
    --network host \
    --cap-add NET_ADMIN \
    --cap-add SYS_MODULE \
    --device /dev/net/tun:/dev/net/tun \
    -v "${STATE_VOLUME}:/var/lib/tailscale" \
    -v /dev/net/tun:/dev/net/tun \
    -e TS_AUTHKEY="${TS_AUTHKEY}" \
    -e TS_ROUTES="${SUBNET}" \
    -e TS_USERSPACE=false \
    -e TS_STATE_DIR=/var/lib/tailscale \
    -e TS_EXTRA_ARGS="--advertise-routes=${SUBNET}" \
    tailscale/tailscale:latest

echo "Tailscale gateway started (container: ${CONTAINER_NAME})."
echo ""
echo "NEXT STEPS:"
echo "  1. Check status  : docker logs -f ${CONTAINER_NAME}"
echo "  2. Approve routes: https://login.tailscale.com/admin/machines"
echo "     (or set autoApprovers in your ACL policy to skip this step)"
