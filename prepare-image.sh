#!/usr/bin/env bash
# prepare-image.sh
# Run this ONCE on a freshly-flashed Raspberry Pi to bake in everything the
# Tailscale gateway needs — Docker, the tailscale image, the gateway service,
# and the first-boot auth-key prompt — WITHOUT an auth key.
#
# Afterward, shut the Pi down and capture the SD card to a .img (see
# BUILD-IMAGE.md). Every Pi flashed with that image will prompt for a key on
# first SSH login and then join your tailnet automatically.
#
# Usage:
#   sudo bash prepare-image.sh
#
# Options:
#   --no-clean   Skip the de-personalization step (host keys, machine-id, logs).
#                Use only if you do NOT intend to capture/redistribute the image.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLEAN=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-clean) CLEAN=0; shift ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Run as root (sudo bash prepare-image.sh)" >&2
    exit 1
fi

echo "==> Preparing this Pi to become a reusable gateway image..."

# ── 0. Network auto-config tooling ────────────────────────────────────────────
# Used by tailscale-gateway-autonet.sh to sniff and configure DHCP-less networks.
#   tcpdump  - passive capture        arping (iputils) - RFC 5227 ACD probes
#   arp-scan - active host discovery   lldpd  - LLDP/CDP neighbor info
#   ndisc6   - IPv6 router discovery (rdisc6)
echo "--> Installing network auto-config tools..."
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y tcpdump arp-scan lldpd ndisc6 iputils-arping
systemctl enable lldpd 2>/dev/null || true

# ── 1. Install Docker (idempotent) ────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    echo "--> Installing Docker via get.docker.com..."
    curl -fsSL https://get.docker.com | sh
else
    echo "--> Docker already present, skipping."
fi

# Let the default (non-root) login user run docker without sudo.
TARGET_USER="${SUDO_USER:-pi}"
if id "${TARGET_USER}" &>/dev/null; then
    echo "--> Adding ${TARGET_USER} to the docker group..."
    usermod -aG docker "${TARGET_USER}" || true
fi

systemctl enable docker

# ── 2. Pre-pull the Tailscale image so first boot needs no internet to start ──
echo "--> Pre-pulling tailscale/tailscale:latest (baked into the image)..."
docker pull tailscale/tailscale:latest

# ── 3. Install the gateway service WITHOUT an auth key ─────────────────────────
# install.sh enables the service but, with no key present, does NOT start it.
echo "--> Running install.sh (no auth key)..."
bash "${SCRIPT_DIR}/install.sh"

# ── 3b. Pre-build the dashboard image so first boot needs no build/internet ────
echo "--> Building dashboard image (baked into the image)..."
docker build -t tailscale-gateway-dashboard:local /opt/tailscale-gateway-dashboard

# ── 4. Install the first-boot auth-key prompt ─────────────────────────────────
echo "--> Installing first-login auth-key prompt to /etc/profile.d/..."
install -m 644 "${SCRIPT_DIR}/tailscale-gateway-firstrun.sh" \
    /etc/profile.d/tailscale-gateway-firstrun.sh

# Make sure no stray key is present in the image.
rm -f /etc/tailscale-gateway/authkey

echo "==> Bake complete."

# ── 5. De-personalize so every flashed card is unique ─────────────────────────
if [[ "${CLEAN}" -eq 1 ]]; then
    echo "==> Cleaning machine-specific state for imaging..."

    # Remove any tailscale node state so clones don't collide on the tailnet.
    docker rm -f tailscale-gateway 2>/dev/null || true
    docker volume rm tailscale-gateway-state 2>/dev/null || true

    # SSH host keys — regenerated on next boot.
    rm -f /etc/ssh/ssh_host_* 2>/dev/null || true
    # Pi OS regenerates host keys on boot via this service if it exists.
    systemctl enable regenerate_ssh_host_keys.service 2>/dev/null || true

    # Unique machine identity — regenerated on boot when empty.
    : > /etc/machine-id 2>/dev/null || true
    rm -f /var/lib/dbus/machine-id 2>/dev/null || true
    ln -sf /etc/machine-id /var/lib/dbus/machine-id 2>/dev/null || true

    # Logs, apt cache, shell history.
    journalctl --rotate 2>/dev/null || true
    journalctl --vacuum-time=1s 2>/dev/null || true
    find /var/log -type f -exec truncate -s 0 {} + 2>/dev/null || true
    apt-get clean 2>/dev/null || true
    rm -f /root/.bash_history 2>/dev/null || true
    rm -f "/home/${TARGET_USER}/.bash_history" 2>/dev/null || true

    echo "==> Cleanup done."
fi

cat <<EOF

============================================================
  Image is ready to capture.

  Next:
    1. sudo shutdown -h now
    2. Move the SD card to your computer.
    3. Capture + shrink the image (see BUILD-IMAGE.md).

  Every Pi flashed from this image will, on first SSH login,
  prompt for a Tailscale auth key and then join the tailnet.
============================================================
EOF
