# PnP Network Gateway

A plug-and-play Raspberry Pi that acts as a [Tailscale](https://tailscale.com)
subnet router. Flash a Pi, give it an auth key, drop it onto any network — it
boots, detects its local subnet, and advertises that subnet to your tailnet so
you can reach everything on that LAN from anywhere.

The Tailscale node runs in a Docker container, started by a systemd service that
waits for DHCP, auto-detects the interface and subnet, and brings the route up
with kernel-level forwarding.

## How it works

On boot, `tailscale-gateway.service` waits for the network to come online, then
runs `start-tailscale-gateway.sh`, which:

1. Finds the interface holding the default route (the one DHCP configured).
2. Derives the directly-connected subnet for that interface.
3. Reads the auth key from `/etc/tailscale-gateway/authkey`.
4. Starts `tailscale/tailscale` in a host-network container advertising that
   subnet, with state persisted in a named Docker volume.

IP forwarding (required for subnet routing) is enabled via a sysctl drop-in.

A companion watcher service (`tailscale-gateway-watch`) monitors the kernel for
network changes. If you move the Pi to a different LAN, it detects the new
subnet and restarts the gateway to advertise it — no power cycle required. (Each
new subnet still needs route approval; use `autoApprovers` in your ACL so this
happens automatically.)

### Networks without DHCP

If no DHCP lease appears within ~15 seconds, `tailscale-gateway-autonet`
passively sniffs the segment (ARP, directed broadcasts, LLDP/CDP, IPv6 router
advertisements) to infer the subnet and gateway, picks an unused address using
RFC 5227 Address Conflict Detection, configures it tentatively, verifies it can
actually reach the internet, and only then commits (via NetworkManager when
present). This is heuristic — a completely silent segment can't be inferred, and
the netmask defaults to /24 unless a directed broadcast or LLDP indicates
otherwise.

### Status dashboard

A companion container (`tailscale-gateway-dashboard`) serves a small web UI that
shows, at a glance: the device's Tailscale address, the LAN interface, subnet,
gateway, internet status, and a live `arp-scan` of every device on the local
subnet — each linked to `http://<ip>` so you can jump straight to its admin page
over the tailnet.

It binds **only to the Pi's Tailscale interface**, so it's reachable solely by
members of your tailnet — that membership is the access control, so there is no
separate password. Reach it at `http://<device-tailscale-ip>:8088` (or via
MagicDNS, `http://<hostname>:8088`). Do not rebind it to `0.0.0.0` without adding
authentication, as that would expose it to the LAN.

## Repository layout

| File | Purpose |
|------|---------|
| `start-tailscale-gateway.sh`     | Core startup script — detects subnet, runs the container, sets up SNAT. |
| `tailscale-gateway.service`      | systemd unit; starts after `network-online.target`, retries on failure. |
| `tailscale-gateway-watch.sh`     | Watches for network changes and restarts the gateway when the subnet changes (hot-swap between LANs). |
| `tailscale-gateway-watch.service`| systemd unit running the watcher. |
| `tailscale-gateway-autonet.sh`   | DHCP fallback: sniffs a DHCP-less network, infers subnet/gateway, picks a free IP (with conflict detection), and self-configures. |
| `tailscale-gateway-autonet.service`| systemd unit running auto-network setup before the gateway. |
| `dashboard/`                     | Flask app + Dockerfile for the tailnet-only status web UI. |
| `start-tailscale-dashboard.sh`   | Builds (if needed) and runs the dashboard container. |
| `tailscale-gateway-dashboard.service`| systemd unit running the dashboard. |
| `99-ip-forward.conf`             | sysctl drop-in enabling IPv4/IPv6 forwarding. |
| `install.sh`                     | Installs the service onto a running Pi (optionally with a key). |
| `prepare-image.sh`               | Bakes a Pi into a reusable golden image (Docker + deps + service, no key). |
| `tailscale-gateway-firstrun.sh`  | `/etc/profile.d` prompt that asks for an auth key on first SSH login. |
| `BUILD-IMAGE.md`                 | Full walkthrough for building and deploying the custom image. |

## Quick start (single Pi)

Install onto a Pi that already has Docker:

```bash
git clone https://github.com/ausbemer/PnP_Network_Gateway.git
cd PnP_Network_Gateway
sudo bash install.sh --authkey tskey-auth-xxxxxxxxxxxx
```

Check it:

```bash
systemctl status tailscale-gateway
docker logs -f tailscale-gateway
```

Then approve the advertised route in the
[Tailscale admin console](https://login.tailscale.com/admin/machines), or set
`autoApprovers` in your ACL policy to skip that step.

## Reusable image (many Pis)

To build one image you can flash onto any number of Pis — each prompting for its
own auth key on first SSH login — see **[BUILD-IMAGE.md](BUILD-IMAGE.md)**. In
short: run `prepare-image.sh` on a configured Pi, shut down, then capture and
shrink the SD card to a `.img`.

## Security notes

- **Never commit an auth key.** Keys live only at `/etc/tailscale-gateway/authkey`
  (chmod 600) on a running Pi. The `.gitignore` blocks `authkey`, `tskey-*`, and
  `*.img` files.
- Use a **reusable, pre-authorized** key when deploying multiple gateways.

## Requirements

- Raspberry Pi running Raspberry Pi OS Lite (64-bit recommended)
- Docker
- A Tailscale account and auth key
