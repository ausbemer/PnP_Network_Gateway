# Tailscale Gateway Project — Conversation Context

## What we're building

A plug-and-play Raspberry Pi that acts as a Tailscale subnet gateway. The goal is:
- Pi boots, gets a DHCP address, automatically starts a Tailscale container advertising the local subnet
- Eventually: a custom Pi OS image (pre-baked with Docker + all dependencies) so any new Pi can be flashed, given an auth key, and dropped onto any network with zero extra setup

---

## Files already created

All files live in `C:\Users\AEmerson\Claude\Projects\Auto_Tailscale_Gateway\` (the user's connected project folder).

### `start-tailscale-gateway.sh`
The core startup script. Run by systemd after DHCP completes. It:
1. Detects the interface with the default route (`ip route show default`)
2. Gets the subnet for that interface (`ip route show dev <iface> proto kernel`)
3. Reads the auth key from `/etc/tailscale-gateway/authkey`
4. Removes any stale container, then runs `tailscale/tailscale:latest` with:
   - `--network host`
   - `TS_ROUTES=<detected subnet>`
   - `TS_USERSPACE=false` (kernel routing)
   - State persisted in a named Docker volume `tailscale-gateway-state`

### `tailscale-gateway.service`
Systemd unit. Key properties:
- `After=network-online.target docker.service` — waits for DHCP before starting
- `Wants=network-online.target`
- `Type=oneshot` + `RemainAfterExit=yes`
- Retries 3× with 10s gaps on failure
- `ExecStop` runs `tailscale logout` before stopping the container

### `99-ip-forward.conf`
Sysctl drop-in (`/etc/sysctl.d/`) enabling `net.ipv4.ip_forward=1` and `net.ipv6.conf.all.forwarding=1`. Required for subnet routing.

### `install.sh`
One-shot installer. Usage:
```bash
sudo bash install.sh --authkey tskey-auth-...
```
Does: copies script to `/usr/local/bin`, installs systemd unit, applies sysctl, writes auth key to `/etc/tailscale-gateway/authkey` (chmod 600), enables + starts the service.

---

## Deployment walkthrough (already documented)

1. Flash **Raspberry Pi OS Lite 64-bit** via Raspberry Pi Imager, with SSH enabled and hostname set (e.g. `tsgateway`) using the gear ⚙ menu
2. Boot, SSH in: `ssh pi@tsgateway.local`
3. `sudo apt update && sudo apt full-upgrade -y`
4. Install Docker: `curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker pi`
5. `scp` the project folder to the Pi
6. `sudo bash install.sh --authkey tskey-auth-<key>`
7. Verify: `systemctl status tailscale-gateway` and `docker logs -f tailscale-gateway`
8. Approve advertised routes in Tailscale admin console (or configure `autoApprovers` in ACL policy)

---

## Where we left off — next task

The user wants to **create a custom Raspberry Pi OS image** — pre-baked with Docker and all dependencies installed, plus the gateway service configured, so any new Pi can be:
1. Flashed with the custom image
2. Given an auth key
3. Powered on → automatically joins the tailnet and advertises its subnet

### Approach to design

The standard way to build a custom Pi image is one of:

**Option A — `pi-gen`** (Raspberry Pi Foundation's official image builder): clone the `pi-gen` repo, add a custom stage that runs the install steps, build a `.img`. Produces a first-class `.img` file identical in structure to official releases. Complex to set up but very clean output.

**Option B — Flash → configure → shrink → capture**: flash official OS, boot it, run install steps, shut down cleanly, use `pishrink.sh` to shrink the partition, then `dd` the SD card to a `.img` file. Simpler, works on any Linux machine.

**Option C — `CustomPiOS`**: wrapper around pi-gen, easier to add custom modules. Used by projects like OctoPi.

The user should be asked which approach they prefer, or the best default recommended.

**Recommended default: Option B** — it's the most straightforward and doesn't require a full pi-gen build environment. The flow is: get the Pi working per the existing walkthrough (minus the auth key step), then image it.

### Key considerations for the image
- Auth key should NOT be baked into the image (security). The image should boot, prompt for or accept a key on first run.
- A `first-boot.sh` script (run once via systemd, then self-disables) could handle: prompting for auth key over serial/SSH, writing it to `/etc/tailscale-gateway/authkey`, then starting the service.
- Alternatively: image just has everything installed + service enabled, and the user drops an `authkey` file onto the SD card's boot partition (FAT32, accessible from Windows/Mac without mounting Linux FS) before first boot. The first-boot script copies it into place.
- The "drop a file on the boot partition" approach is the most plug-and-play — same UX as `wpa_supplicant.conf` and `ssh` file tricks from older Pi OS versions.

---

## User preferences
- Concise, direct responses
- No unnecessary verbosity
