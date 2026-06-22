#!/usr/bin/env bash
# start-tailscale-media.sh
# Runs Jellyfin as the network video server. Host networking puts it on BOTH the
# LAN and the tailnet at once (port 8096) — safe to expose on the LAN because
# Jellyfin has its own login. Library and config live on the NVMe.

set -euo pipefail

IMAGE="jellyfin/jellyfin:latest"
CONTAINER="tailscale-gateway-media"
MEDIA_ROOT="${MEDIA_ROOT:-/mnt/nvme}"
CONFIG_DIR="${MEDIA_ROOT}/jellyfin/config"
CACHE_DIR="${MEDIA_ROOT}/jellyfin/cache"
LIBRARY_DIR="${MEDIA_ROOT}/media"

# The point of this is to serve from the big SSD — warn loudly if it isn't
# mounted, but still run (config would land on the SD card otherwise).
if ! mountpoint -q "${MEDIA_ROOT}" 2>/dev/null; then
    echo "WARNING: ${MEDIA_ROOT} is not a mounted filesystem — is the NVMe mounted?" >&2
    echo "         Media/config would be stored on the SD card until it is." >&2
fi

mkdir -p "${CONFIG_DIR}" "${CACHE_DIR}" "${LIBRARY_DIR}"

# Pull the image if we don't have it yet (first run needs internet; ~hundreds of MB).
if ! docker image inspect "${IMAGE}" &>/dev/null; then
    echo "Pulling ${IMAGE} (first run)..."
    docker pull "${IMAGE}"
fi

# Best-effort hardware-decode passthrough (Pi 5 can DECODE; it has no encoder, so
# transcoding stays on the CPU — keep your library direct-play-friendly).
devs=()
for d in /dev/dri /dev/video10 /dev/video11 /dev/video12 /dev/video18 /dev/video19; do
    if [ -e "${d}" ]; then devs+=(--device "${d}"); fi
done

docker rm -f "${CONTAINER}" &>/dev/null || true

echo "Starting Jellyfin (port 8096, LAN + tailnet)..."
docker run -d \
    --name "${CONTAINER}" \
    --restart unless-stopped \
    --network host \
    -e TZ="$(cat /etc/timezone 2>/dev/null || echo UTC)" \
    -v "${CONFIG_DIR}:/config" \
    -v "${CACHE_DIR}:/cache" \
    -v "${LIBRARY_DIR}:/media" \
    "${devs[@]}" \
    "${IMAGE}"

echo "Jellyfin started. Reach it at:"
echo "  http://<this-device-LAN-ip>:8096        (local)"
echo "  http://<this-device-tailscale-ip>:8096  (remote, over Tailscale)"
echo "Put video files in ${LIBRARY_DIR} (or upload via the dashboard file explorer)."
