#!/usr/bin/env bash
# start-tailscale-radarr.sh
# Runs Radarr as a library manager for the media server. Bound TAILNET-ONLY
# (published on the Tailscale interface IP) because it's an admin tool — same
# trust model as the dashboard.
#
# NOTE: this sets up the Radarr app only. It does NOT configure indexers or a
# download client. Point it at content you legally own / are licensed to use.

set -euo pipefail

IMAGE="lscr.io/linuxserver/radarr:latest"
CONTAINER="tailscale-gateway-radarr"
MEDIA_ROOT="${MEDIA_ROOT:-/mnt/nvme}"
CONFIG_DIR="${MEDIA_ROOT}/radarr/config"
MOVIES_DIR="${MEDIA_ROOT}/media"          # shared with Jellyfin
DOWNLOADS_DIR="${MEDIA_ROOT}/downloads"   # manual-import folder
PORT="${RADARR_PORT:-7878}"
TS_IFACE="${TS_IFACE:-tailscale0}"

TARGET_USER="${SUDO_USER:-pi}"
PUID="$(id -u "${TARGET_USER}" 2>/dev/null || echo 1000)"
PGID="$(id -g "${TARGET_USER}" 2>/dev/null || echo 1000)"

if ! mountpoint -q "${MEDIA_ROOT}" 2>/dev/null; then
    echo "WARNING: ${MEDIA_ROOT} is not mounted — is the NVMe mounted?" >&2
fi
mkdir -p "${CONFIG_DIR}" "${MOVIES_DIR}" "${DOWNLOADS_DIR}"

# Wait for the Tailscale interface to have an IP so we can bind only to it.
tsip=""
for _ in $(seq 1 30); do
    tsip="$(ip -4 -o addr show "${TS_IFACE}" 2>/dev/null \
        | grep -oE 'inet [0-9.]+' | awk '{print $2}' | head -1)"
    [[ -n "${tsip}" ]] && break
    sleep 2
done

if [[ -n "${tsip}" ]]; then
    publish=(-p "${tsip}:${PORT}:${PORT}")
    echo "Binding Radarr to tailnet only: ${tsip}:${PORT}"
else
    publish=(-p "${PORT}:${PORT}")
    echo "WARNING: ${TS_IFACE} has no IP — publishing on ALL interfaces." >&2
    echo "         Radarr will be on the LAN too; set up its login immediately." >&2
fi

if ! docker image inspect "${IMAGE}" &>/dev/null; then
    echo "Pulling ${IMAGE} (first run)..."
    docker pull "${IMAGE}"
fi

docker rm -f "${CONTAINER}" &>/dev/null || true

docker run -d \
    --name "${CONTAINER}" \
    --restart unless-stopped \
    -e PUID="${PUID}" -e PGID="${PGID}" \
    -e TZ="$(cat /etc/timezone 2>/dev/null || echo UTC)" \
    -v "${CONFIG_DIR}:/config" \
    -v "${MOVIES_DIR}:/movies" \
    -v "${DOWNLOADS_DIR}:/downloads" \
    "${publish[@]}" \
    "${IMAGE}"

echo "Radarr started on port ${PORT} (tailnet-only)."
echo "Library: ${MOVIES_DIR} -> /movies   ·   Config: ${CONFIG_DIR}"
