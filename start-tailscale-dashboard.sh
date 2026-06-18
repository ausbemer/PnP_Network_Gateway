#!/usr/bin/env bash
# start-tailscale-dashboard.sh
# Builds (if needed) and runs the tailnet-only status dashboard container.
# The Flask app inside binds itself to the Tailscale interface address, so the
# dashboard is reachable only from your tailnet — never from the LAN.

set -euo pipefail

IMAGE="tailscale-gateway-dashboard:local"
CONTAINER="tailscale-gateway-dashboard"
SRC_DIR="/opt/tailscale-gateway-dashboard"
PORT="${DASHBOARD_PORT:-8088}"
TS_IFACE="${TS_IFACE:-tailscale0}"

# Boot (FAT) partition holds autonet.log; mount it read-only so the dashboard
# can display it. Path differs across Pi OS versions.
BOOT_DIR="/boot/firmware"
[[ -d "${BOOT_DIR}" ]] || BOOT_DIR="/boot"

# ── Build the image every start (Docker layer cache makes this a near-instant ──
# ── no-op when nothing changed, but it ensures app.py updates are picked up   ──
# ── after a `git pull` + reinstall). ──────────────────────────────────────────
echo "Building dashboard image from ${SRC_DIR} (cached if unchanged)..."
docker build -t "${IMAGE}" "${SRC_DIR}"

# ── Replace any stale container (idempotent restart) ──────────────────────────
if docker inspect "${CONTAINER}" &>/dev/null; then
    docker rm -f "${CONTAINER}" >/dev/null
fi

echo "Starting dashboard (port ${PORT}, bound to ${TS_IFACE})..."
docker run -d \
    --name "${CONTAINER}" \
    --restart unless-stopped \
    --network host \
    --cap-add NET_RAW \
    --cap-add NET_ADMIN \
    -e DASHBOARD_PORT="${PORT}" \
    -e TS_IFACE="${TS_IFACE}" \
    -e AUTONET_LOG="/bootfw/autonet.log" \
    -v "${BOOT_DIR}:/bootfw:rw" \
    "${IMAGE}"

echo "Dashboard started. Once Tailscale is up, browse to:"
echo "  http://<this-device-tailscale-ip>:${PORT}"
echo "  (or http://${HOSTNAME}:${PORT} via MagicDNS)"
