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

**Images in the rotation:** upload any `.png`/`.bmp`/`.jpg` through the
dashboard's file explorer into the **`oled-images`** folder (it lives on the NVMe
at `/mnt/nvme/oled-images`, which is what the explorer is rooted on). The daemon
converts each to 1-bit and cycles it in alongside the status pages. Bold,
high-contrast art works best on a 1-bit 128×64 panel; the daemon auto-fits and
centers it (threshold tunable via `OLED_IMG_THRESHOLD`, invert via a filename
containing `invert`). If the `oled-images` folder isn't there yet, use the file
explorer's **Create folder** button to make it.

The dashboard also shows the **autonet log** (the `autonet log →` link), reading
`autonet.log` from the boot partition. `autonet` writes that file on every run,
so you can diagnose a no-DHCP boot either over the tailnet (success) or by
pulling the SD card and reading the FAT partition directly (failure).

### Network load testing

A bounded test instrument for finding where your network equipment starts to
react under broadcast load. It sends broadcast ARP frames at a **rate-limited,
steadily increasing** pace and, at each step, measures the network's reaction so
you can see the rate at which storm-control or CPU limits kick in. It reports
every rate, has a hard pps ceiling, and stops on demand — it is a measurement
tool, not a flood. **Use only on networks you own or are authorized to test.**

**From the dashboard.** The **Network load test** panel runs the ramp and, at
each step, captures a time series of:

- **target pps** vs **achieved pps** — the gap reveals whether the *Pi* is the
  bottleneck (the raw-socket sender topping out) rather than the network. If
  achieved plateaus well below target, you're measuring the sender, not the
  switch.
- **latency (avg)**, **jitter** (ping `mdev`), and **packet loss** to **two
  targets at once**: the **gateway** (same broadcast domain) and an **internet
  target** (default `1.1.1.1`). A quiet gateway but a struggling internet path
  points at the gateway's control plane choking on broadcasts; loss/jitter on
  the gateway points at the local switch.

Every sample is logged to a per-run CSV on the NVMe at
`/mnt/nvme/loadtest/loadtest-<timestamp>.csv` (browsable/downloadable from the
file explorer), and the **📈 chart →** link opens a live, auto-refreshing graph
of pps and the reaction metrics over time. Look for the *knee* — the step where
latency/jitter/loss inflects is where your equipment starts to struggle.

**From the CLI** (`tsg-broadcast-ramp`, gateway-only metrics):

```bash
# preview the ramp plan (sends nothing):
sudo tsg-broadcast-ramp --dry-run

# ramp 100 -> 2000 pps in +100 steps, 10s each, logging metrics to CSV:
sudo tsg-broadcast-ramp --start 100 --max 2000 --step 100 --step-secs 10 --csv ramp.csv
```

## Repository layout

Each component lives in its own directory with its script(s) and systemd unit:

| Path | Purpose |
|------|---------|
| `gateway/`     | Core subnet router — `start-tailscale-gateway.sh` (detect subnet, run the container, set up SNAT), `tailscale-gateway.service`, and `99-ip-forward.conf` (IP-forwarding sysctl). |
| `autonet/`     | DHCP-fallback auto-networking — `tailscale-gateway-autonet.sh` (sniff/infer/multi-home a DHCP-less segment) and its unit. |
| `watch/`       | Hot-swap watcher — `tailscale-gateway-watch.sh` (re-advertise when the network changes) and its unit. |
| `dashboard/`   | Tailnet-only web UI — `app.py`, `Dockerfile`, `requirements.txt`, `start-tailscale-dashboard.sh`, `tailscale-gateway-dashboard.service`. |
| `oled/`        | OLED status display — `tailscale-gateway-oled.py`, the `tsg-oled` helper, `tailscale-gateway-oled.service`, and `images/`. |
| `firstboot/`   | `tailscale-gateway-firstrun.sh` — first-SSH auth-key prompt (installed to `/etc/profile.d`). |
| `loadtest/`    | `broadcast-ramp.py` — bounded broadcast load-ramp test tool (installed as `tsg-broadcast-ramp`; manual, no service). |
| `docs/`        | `BUILD-IMAGE.md` and other documentation. |
| `install.sh`   | Installs/updates all of the above onto a running Pi (optionally with `--authkey`). |
| `prepare-image.sh` | Bakes a Pi into a reusable golden image (Docker + deps + services, no key). |

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

### Using an OAuth client (Trust Credential) instead of an auth key

For a fleet, an OAuth client secret is nicer than a static key: one secret works
for every Pi, and the nodes come up tagged and non-expiring. Create the client in
the Tailscale admin console with the **minimum scope: `Auth Keys` → Write** (the
`Devices`/Core scope is *not* needed — that's for the API), and assign it a tag
(e.g. `tag:auto-gateway`).

Why a tag is mandatory for OAuth: the **key authorizes the device to join**, but
every node also needs an **owner/identity inside the tailnet** — that's the tag,
and it's what your ACL rules and `autoApprovers` are written against. A normal
auth key is owned by the user who made it; an OAuth client has no user, so the
node must be tagged instead. Pass it explicitly (nothing is hardcoded):

```bash
sudo bash install.sh --authkey tskey-client-XXXXXXXX --tags tag:auto-gateway
```

`--tags` is **required** for OAuth secrets (`tskey-client-…`) and optional
otherwise. Two opt-in flags control key properties; omit them to leave
Tailscale's defaults:

| Flag | Effect | Note |
|------|--------|------|
| `--ephemeral true\|false` | sets the node's ephemeral flag | OAuth defaults this to **true** — for an always-on gateway you usually want `false`, or the node is auto-removed minutes after a reboot |
| `--preauth true\|false` | skip manual device approval | OAuth defaults to `false`; set `true` for hands-off bring-up if device approval is on |

Your ACL policy needs a matching `tagOwners` entry (e.g.
`"tag:auto-gateway": ["autogroup:admin"]`) and, for hands-off routing, an
`autoApprovers` entry for the advertised subnets referencing that tag.

## Reusable image (many Pis)

To build one image you can flash onto any number of Pis — each prompting for its
own auth key on first SSH login — see **[docs/BUILD-IMAGE.md](docs/BUILD-IMAGE.md)**. In
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
