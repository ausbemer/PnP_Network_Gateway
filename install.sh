#!/usr/bin/env bash
# install.sh
# Installs the Tailscale gateway service onto a Raspberry Pi (or any
# Debian-based system with Docker installed).
#
# Usage:
#   sudo bash install.sh [--authkey tskey-auth-...]
#
# Options:
#   --authkey <key>   Write the key to /etc/tailscale-gateway/authkey.
#                     Omit if you plan to add the key file manually.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTHKEY=""

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --authkey)
            AUTHKEY="$2"
            shift 2
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

# ── Require root ──────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Run this script as root (sudo bash install.sh)" >&2
    exit 1
fi

# ── Check dependencies ────────────────────────────────────────────────────────
for cmd in docker ip systemctl; do
    if ! command -v "${cmd}" &>/dev/null; then
        echo "ERROR: '${cmd}' not found. Please install it and re-run." >&2
        exit 1
    fi
done

echo "==> Installing Tailscale gateway..."

# ── 1. Startup script ─────────────────────────────────────────────────────────
echo "--> Copying start-tailscale-gateway.sh to /usr/local/bin/"
install -m 755 "${SCRIPT_DIR}/start-tailscale-gateway.sh" /usr/local/bin/start-tailscale-gateway.sh

# ── 2. Systemd unit ───────────────────────────────────────────────────────────
echo "--> Installing systemd unit tailscale-gateway.service"
install -m 644 "${SCRIPT_DIR}/tailscale-gateway.service" /etc/systemd/system/tailscale-gateway.service

# ── 3. IP forwarding ─────────────────────────────────────────────────────────
echo "--> Enabling IP forwarding"
install -m 644 "${SCRIPT_DIR}/99-ip-forward.conf" /etc/sysctl.d/99-ip-forward.conf
sysctl -p /etc/sysctl.d/99-ip-forward.conf

# ── 4. Auth key (optional) ────────────────────────────────────────────────────
mkdir -p /etc/tailscale-gateway
chmod 700 /etc/tailscale-gateway

if [[ -n "${AUTHKEY}" ]]; then
    echo "--> Writing auth key to /etc/tailscale-gateway/authkey"
    echo "${AUTHKEY}" > /etc/tailscale-gateway/authkey
    chmod 600 /etc/tailscale-gateway/authkey
else
    if [[ ! -f /etc/tailscale-gateway/authkey ]]; then
        echo ""
        echo "NOTICE: No auth key provided."
        echo "        Create /etc/tailscale-gateway/authkey before starting the service:"
        echo "          echo 'tskey-auth-...' | sudo tee /etc/tailscale-gateway/authkey"
        echo "          sudo chmod 600 /etc/tailscale-gateway/authkey"
        echo ""
    fi
fi

# ── 5. Enable and start the service ──────────────────────────────────────────
echo "--> Enabling tailscale-gateway.service"
systemctl daemon-reload
systemctl enable tailscale-gateway.service

if [[ -f /etc/tailscale-gateway/authkey ]]; then
    echo "--> Starting tailscale-gateway.service"
    systemctl start tailscale-gateway.service
    echo ""
    echo "==> Done. Check status with:"
    echo "      systemctl status tailscale-gateway"
    echo "      docker logs -f tailscale-gateway"
else
    echo ""
    echo "==> Service enabled but NOT started (no auth key yet)."
    echo "    Add your key, then run: sudo systemctl start tailscale-gateway"
fi
