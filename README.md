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
otherwise. To preview what it would do without changing anything, run
`sudo tailscale-gateway-autonet.sh --dry-run`.

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

### File explorer (NVMe)

The dashboard includes a file browser (the **files →** link) scoped to a storage
mount — intended for the Argon V5's NVMe SSD. It lists folders/files with sizes,
shows total/used/free space, and supports download, upload, create-folder, and
delete. Access is tailnet-only (same trust model as the rest of the dashboard);
paths are strictly contained to the mount (no `../` escapes, symlinks resolving
outside are rejected).

Set up the SSD on the host first (the explorer just browses whatever is mounted):

```bash
lsblk                                   # find the NVMe (e.g. /dev/nvme0n1) and its size
sudo mkfs.ext4 /dev/nvme0n1             # ONLY if blank — this erases the disk
sudo mkdir -p /mnt/nvme
sudo mount /dev/nvme0n1 /mnt/nvme
# persist across reboots:
echo "/dev/nvme0n1  /mnt/nvme  ext4  defaults,nofail  0  2" | sudo tee -a /etc/fstab
```

The dashboard mounts `/mnt/nvme` into the container as the explorer root (override
with `NVME_MOUNT=/path` before start). If `lsblk` doesn't show the NVMe on a Pi 5,
check the FFC cable and that PCIe is enabled (`dtparam=pcie` / Gen-3 via
`dtparam=pcie_gen=3` in `config.txt`).

### Media server (Jellyfin)

With storage on the NVMe, the gateway can double as a video server. Jellyfin runs
in a container with **host networking**, so it's reachable on **both** the LAN
(`http://<lan-ip>:8096`) and the tailnet (`http://<tailscale-ip>:8096`, or the
dashboard's **media →** link) at once. Unlike the admin dashboard, this is safe to
expose on the LAN because Jellyfin has its own login. Library/config live on the
NVMe (`/mnt/nvme/media`, `/mnt/nvme/jellyfin`); add videos there or via the file
explorer.

Note on the Pi 5: it can hardware-*decode* but has **no hardware encoder**, so
real-time transcoding is CPU-only (~one 1080p stream). Keep the library in
direct-play formats (H.264/AAC MP4) for smooth playback locally and remotely;
4K/high-bitrate transcoding will struggle.

### OLED status display (Argon One V5)

If the Pi is in an Argon One V5 with the OLED module (SSD1306 @ `0x3c`), the
`tailscale-gateway-oled` service drives it directly with `luma.oled`, cycling
through hostname, Tailscale IP, internet status, gateway, and the connected
subnets. Any part of the program can flash a transient message to it with the
`tsg-oled` helper, e.g. `tsg-oled "autonet" "via 172.30.0.1"`; the daemon shows
it for ~25s then resumes the rotating pages.

Requirements: enable I2C (`raspi-config nonint do_i2c 0`) and install
`i2c-tools python3-luma.oled python3-pil` (the image does both). **Disable
Argon's own OLED screen** (in `argonone-config`) so it doesn't fight us for the
I2C bus — Argon's fan control can stay.

**Images in the rotation:** drop any `.png`/`.bmp`/`.jpg` into
`/boot/firmware/oled-images/` (it's the FAT boot partition, so you can add them
by popping the SD into any computer) and the daemon converts each to 1-bit and
cycles it in alongside the status pages. Bold, high-contrast art works best on a
1-bit 128×64 panel; the daemon auto-fits and centers it (threshold tunable via
`OLED_IMG_THRESHOLD`).

The dashboard also shows the **autonet log** (the `autonet log →` link), reading
`autonet.log` from the boot partition. `autonet` writes that file on every run,
so you can diagnose a no-DHCP boot either over the tailnet (success) or by
pulling the SD card and reading the FAT partition directly (failure).

## Repository layout

| File | Purpose |
|------|---------|
| `start-tailscale-gateway.sh`     | Core startup script — detects subnet, runs the container, sets up SNAT. |
| `tailscale-gateway.service`      | systemd unit; starts after `network-online.target`, retries on failure. |
| `tailscale-gateway-watch.sh`     | Watches for network changes and restarts the gateway when the subnet changes (hot-swap between LANs). |
| `tailscale-gateway-watch.service`| systemd unit running the watcher. |
| `tailscale-gateway-autonet.sh`   | DHCP fallback: sniffs a DHCP-less network, infers subnet/gateway, picks a free IP (with conflict detection), and self-configures. |
| `tailscale-gateway-autonet.service`| systemd unit running auto-network setup before the gateway. |
| `oled/`                          | OLED status daemon + `tsg-oled` write helper (Argon One V5 / SSD1306). |
| `tailscale-gateway-oled.service` | systemd unit running the OLED daemon. |
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
