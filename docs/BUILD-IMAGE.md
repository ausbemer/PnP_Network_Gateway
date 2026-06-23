# Building a reusable Tailscale Gateway Pi image

This is the **Option B** workflow: take one Pi, bake everything in, then capture
the SD card to a `.img` you can flash onto any number of Pis. Each clone prompts
for a Tailscale auth key on its **first SSH login** and then joins your tailnet
automatically — no auth key is ever stored in the image.

---

## Part 1 — Build the golden image (do this once)

### 1. Flash a clean base OS
Use **Raspberry Pi Imager** with **Raspberry Pi OS Lite (64-bit)**. In the
gear (⚙) menu before writing:
- Enable **SSH** (password or public key)
- Set a **hostname** (e.g. `tsgateway`)
- Set a username/password (these become the image default — pick deliberately)

### 2. Boot and copy the project onto the Pi
```bash
ssh <user>@tsgateway.local
sudo apt update && sudo apt full-upgrade -y
```
From your computer, copy the project folder over (run on your machine):
```bash
scp -r /path/to/Auto_Tailscale_Gateway <user>@tsgateway.local:~/
```

### 3. Bake everything in
On the Pi:
```bash
cd ~/Auto_Tailscale_Gateway
sudo bash prepare-image.sh
```
This installs Docker, pre-pulls `tailscale/tailscale:latest`, installs the
gateway service (enabled but **not** started — no key yet), installs the
first-login key prompt, then de-personalizes the system for cloning.

### 4. Shut down cleanly
```bash
sudo shutdown -h now
```
Wait for the green LED activity to stop, then pull the SD card and put it in
your computer.

### 5. Capture and shrink the image

**On Linux / WSL / Raspberry Pi:**
```bash
# Find the device (e.g. /dev/sdb or /dev/mmcblk0) — be certain before dd!
lsblk

# Capture the whole card to a file (replace /dev/sdX with your card).
sudo dd if=/dev/sdX of=tsgateway.img bs=4M status=progress
sync

# Shrink so the .img is only as large as the data, and auto-expands on
# first boot of each clone.
wget https://raw.githubusercontent.com/Drewsif/PiShrink/master/pishrink.sh
chmod +x pishrink.sh
sudo ./pishrink.sh tsgateway.img tsgateway-shrunk.img
```

`tsgateway-shrunk.img` is your distributable image.

> **macOS / Windows note:** `dd` + `pishrink` need a Linux environment. On
> Windows use WSL2 (with usbipd to pass the card reader through) or Win32 Disk
> Imager to capture, then shrink under WSL. On macOS use `diskutil list` to find
> the disk and `sudo dd if=/dev/rdiskN of=tsgateway.img bs=4m`, then shrink on a
> Linux box. PiShrink only runs on Linux.

---

## Part 2 — Deploy a new gateway (repeat per Pi)

1. Flash `tsgateway-shrunk.img` onto an SD card with Raspberry Pi Imager
   (choose **Use custom** → select the `.img`). The partition auto-expands to
   fill the card on first boot.
2. Optionally set a unique hostname in the Imager gear menu so multiple
   gateways don't all answer to `tsgateway.local`.
3. Insert the card, connect Ethernet (or pre-configure Wi-Fi), power on.
4. SSH in:
   ```bash
   ssh <user>@<hostname>.local
   ```
5. You'll be prompted to paste a Tailscale auth key. Paste it. The Pi writes
   the key, starts the service, and begins advertising its local subnet.
6. Approve the advertised route in the
   [Tailscale admin console](https://login.tailscale.com/admin/machines)
   — or set `autoApprovers` in your ACL policy to skip this step every time.

If you press Enter to skip the prompt, run `tsg-setup` later to configure it.

---

## Notes

- **No key in the image.** The auth key only ever lives at
  `/etc/tailscale-gateway/authkey` on a running Pi (chmod 600), written at
  first login. Re-imaging or recapturing never captures a key, because
  `prepare-image.sh` deletes it.
- **Reusable keys recommended.** Since every clone needs a key, generate a
  *reusable, pre-authorized* key (and optionally tag it) in the Tailscale
  admin console so you can reuse it across deployments.
- **Unique identities.** `prepare-image.sh` clears SSH host keys, `machine-id`,
  and the tailscale state volume so clones don't collide. Each Pi regenerates
  these on first boot.
- **Updating the image.** To ship a new version, flash the golden image (or the
  base OS) onto a Pi, re-run `prepare-image.sh`, and recapture.
