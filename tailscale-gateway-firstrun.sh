# /etc/profile.d/tailscale-gateway-firstrun.sh
# Prompts for a Tailscale auth key on the first interactive SSH/console login,
# then starts the gateway and never prompts again.
#
# This file is SOURCED by login shells (it is a profile.d snippet), so it must
# not `exit` the shell and must stay quiet for non-interactive sessions.

# Only run for interactive shells (skip scp/rsync/cron/etc.).
case "$-" in
    *i*) ;;
    *) return 2>/dev/null || true ;;
esac

__tsg_firstrun() {
    local authkey_file="/etc/tailscale-gateway/authkey"
    local key=""

    # Already configured? Do nothing.
    if sudo test -s "${authkey_file}" 2>/dev/null; then
        return 0
    fi

    cat <<'BANNER'

============================================================
  Tailscale Gateway — first-time setup
============================================================
  This Pi has the gateway pre-installed but needs an auth
  key to join your tailnet.

  Get a key at:
    https://login.tailscale.com/admin/settings/keys
  (a reusable, pre-authorized key is recommended)
============================================================

BANNER

    # Prompt until we get something that looks like a key, or the user bails.
    while true; do
        read -r -p "Paste your Tailscale auth key (or press Enter to skip): " key
        if [ -z "${key}" ]; then
            echo "Skipped. Re-open a session or run 'tsg-setup' to configure later."
            return 0
        fi
        case "${key}" in
            tskey-*) break ;;
            *) echo "  That doesn't look like a Tailscale key (should start with 'tskey-'). Try again." ;;
        esac
    done

    echo "--> Saving key..."
    sudo install -d -m 700 /etc/tailscale-gateway
    printf '%s\n' "${key}" | sudo tee "${authkey_file}" >/dev/null
    sudo chmod 600 "${authkey_file}"

    echo "--> Starting tailscale-gateway.service..."
    if sudo systemctl start tailscale-gateway.service; then
        echo ""
        echo "Done. The Pi is joining your tailnet and advertising its subnet."
        echo "  Watch logs : docker logs -f tailscale-gateway"
        echo "  Approve the advertised route in the Tailscale admin console:"
        echo "    https://login.tailscale.com/admin/machines"
        echo ""
    else
        echo "Service failed to start. Inspect with:"
        echo "  systemctl status tailscale-gateway"
        echo "  journalctl -u tailscale-gateway -e"
    fi
}

# Provide a re-runnable command in case the user skips the prompt.
alias tsg-setup='__tsg_firstrun'

__tsg_firstrun
