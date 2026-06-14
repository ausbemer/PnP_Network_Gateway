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

# ── 1. Startup + watcher scripts ──────────────────────────────────────────────
echo "--> Copying start-tailscale-gateway.sh to /usr/local/bin/"
install -m 755 "${SCRIPT_DIR}/start-tailscale-gateway.sh" /usr/local/bin/start-tailscale-gateway.sh

echo "--> Copying tailscale-gateway-watch.sh to /usr/local/bin/"
install -m 755 "${SCRIPT_DIR}/tailscale-gateway-watch.sh" /usr/local/bin/tailscale-gateway-watch.sh

echo "--> Copying tailscale-gateway-autonet.sh to /usr/local/bin/"
install -m 755 "${SCRIPT_DIR}/tailscale-gateway-autonet.sh" /usr/local/bin/tailscale-gateway-autonet.sh

echo "--> Copying start-tailscale-dashboard.sh to /usr/local/bin/"
install -m 755 "${SCRIPT_DIR}/start-tailscale-dashboard.sh" /usr/local/bin/start-tailscale-dashboard.sh

echo "--> Installing dashboard app to /opt/tailscale-gateway-dashboard/"
install -d -m 755 /opt/tailscale-gateway-dashboard
install -m 644 "${SCRIPT_DIR}/dashboard/app.py"          /opt/tailscale-gateway-dashboard/app.py
install -m 644 "${SCRIPT_DIR}/dashboard/requirements.txt" /opt/tailscale-gateway-dashboard/requirements.txt
install -m 644 "${SCRIPT_DIR}/dashboard/Dockerfile"      /opt/tailscale-gateway-dashboard/Dockerfile

# ── 2. Systemd units ──────────────────────────────────────────────────────────
echo "--> Installing systemd unit tailscale-gateway.service"
install -m 644 "${SCRIPT_DIR}/tailscale-gateway.service" /etc/systemd/system/tailscale-gateway.service

echo "--> Installing systemd unit tailscale-gateway-watch.service"
install -m 644 "${SCRIPT_DIR}/tailscale-gateway-watch.service" /etc/systemd/system/tailscale-gateway-watch.service

echo "--> Installing systemd unit tailscale-gateway-autonet.service"
install -m 644 "${SCRIPT_DIR}/tailscale-gateway-autonet.service" /etc/systemd/system/tailscale-gateway-autonet.service

echo "--> Installing systemd unit tailscale-gateway-dashboard.service"
install -m 644 "${SCRIPT_DIR}/tailscale-gateway-dashboard.service" /etc/systemd/system/tailscale-gateway-dashboard.service

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

# ── 5. Enable and start the services ─────────────────────────────────────────
echo "--> Enabling tailscale-gateway services"
systemctl daemon-reload
systemctl enable tailscale-gateway-autonet.service
systemctl enable tailscale-gateway.service
systemctl enable tailscale-gateway-watch.service
systemctl enable tailscale-gateway-dashboard.service

if [[ -f /etc/tailscale-gateway/authkey ]]; then
    # Use restart (not start) so re-running install.sh after a `git pull`
    # actually redeploys: oneshot units that are already "active" ignore start,
    # and the dashboard/gateway start scripts recreate their containers (and the
    # dashboard rebuilds its image) on restart.
    echo "--> (Re)starting tailscale-gateway-autonet.service"
    systemctl restart tailscale-gateway-autonet.service || true
    echo "--> (Re)starting tailscale-gateway.service"
    systemctl restart tailscale-gateway.service
    echo "--> (Re)starting tailscale-gateway-watch.service"
    systemctl restart tailscale-gateway-watch.service
    echo "--> (Re)starting tailscale-gateway-dashboard.service"
    systemctl restart tailscale-gateway-dashboard.service
    echo ""
    echo "==> Done. Check status with:"
    echo "      systemctl status tailscale-gateway"
    echo "      systemctl status tailscale-gateway-watch"
    echo "      systemctl status tailscale-gateway-dashboard"
    echo "      docker logs -f tailscale-gateway"
else
    echo ""
    echo "==> Services enabled but NOT started (no auth key yet)."
    echo "    Add your key, then run:"
    echo "      sudo systemctl start tailscale-gateway tailscale-gateway-watch"
fi
