#!/usr/bin/env python3
"""
OLED status display for the Tailscale gateway (Argon One V5 / SSD1306 @ 0x3c).

Cycles through auto-derived diagnostics (hostname, Tailscale IP, internet,
gateway, subnets) and shows any transient message pushed by the rest of the
program via the `tsg-oled` helper (which writes /run/tailscale-gateway/oled.msg).

Drive the panel ourselves with luma.oled — do NOT also run Argon's OLED daemon,
or the two will fight over the I2C bus. Keep Argon's fan control; just disable
its screen in `argonone-config`.
"""
import os
import re
import socket
import subprocess
import time

from luma.core.interface.serial import i2c
from luma.oled.device import ssd1306
from luma.core.render import canvas
from PIL import Image, ImageOps

I2C_PORT = int(os.environ.get("OLED_I2C_PORT", "1"))
I2C_ADDR = int(os.environ.get("OLED_I2C_ADDR", "0x3c"), 16)
MSG_FILE = os.environ.get("OLED_MSG_FILE", "/run/tailscale-gateway/oled.msg")
PAGE_SECS = float(os.environ.get("OLED_PAGE_SECS", "5"))
MSG_TTL = float(os.environ.get("OLED_MSG_TTL", "25"))
LINE_H = 11          # pixels per line; ~5-6 lines on a 128x64 panel
MAX_COLS = 21        # chars per line at the default font on 128px
# Drop .png/.bmp/.jpg images here to add them to the rotation. On a Pi this is
# the FAT boot partition, so you can add images by popping the SD in any computer.
IMAGE_DIR = os.environ.get("OLED_IMAGE_DIR", "/boot/firmware/oled-images")
IMG_THRESHOLD = int(os.environ.get("OLED_IMG_THRESHOLD", "128"))  # 0-255 b/w cutoff
# Invert all images by default? Per-image override: put "invert" in the filename
# (e.g. "s-invert.png") to flip just that one — black art on a white background.
IMG_INVERT_DEFAULT = os.environ.get("OLED_IMG_INVERT", "0") not in ("0", "", "false", "no")


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""


def ts_ip():
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)",
                  run(["ip", "-4", "-o", "addr", "show", "tailscale0"]))
    return m.group(1) if m else None


def default_iface_gw():
    out = run(["ip", "route", "show", "default"])
    mi = re.search(r"dev (\S+)", out)
    mg = re.search(r"via (\d+\.\d+\.\d+\.\d+)", out)
    return (mi.group(1) if mi else None, mg.group(1) if mg else None)


def subnets(iface):
    if not iface:
        return []
    out = run(["ip", "-4", "-o", "route", "show", "dev", iface, "proto", "kernel"])
    return re.findall(r"(\d+\.\d+\.\d+\.\d+/\d+)", out)


def local_ips(iface):
    """The device's own global IPv4 address(es) — for local SSH."""
    if not iface:
        return []
    out = run(["ip", "-4", "-o", "addr", "show", "dev", iface, "scope", "global"])
    return re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out)


def internet():
    for hp in (("1.1.1.1", 443), ("8.8.8.8", 443)):
        try:
            with socket.create_connection(hp, timeout=3):
                return True
        except OSError:
            continue
    return False


def standing_pages():
    iface, gw = default_iface_gw()
    lips = local_ips(iface)
    subs = subnets(iface)
    pages = [[
        socket.gethostname(),
        "LAN: " + (lips[0] if lips else "(none)"),
        "TS : " + (ts_ip() or "offline"),
        "Net: " + ("up" if internet() else "DOWN"),
    ], [
        "Gateway:",
        gw or "(none)",
        "Iface: " + (iface or "-"),
    ]]
    if len(lips) > 1:   # multi-homed — show every address you could SSH to
        pages.append(["This device IPs:"] + lips[:5])
    if subs:
        pages.append(["Subnets (%d):" % len(subs)] + subs[:5])
    return pages


def message_lines():
    """Lines to show from the push file, honoring its header line:
       'sticky'        -> show until cleared (dashboard messages)
       <expiry epoch>  -> show until that time (tsg-oled messages)
    A header-less file is treated as legacy and shown for MSG_TTL by mtime."""
    try:
        with open(MSG_FILE) as f:
            raw = [ln.rstrip("\n") for ln in f]
    except OSError:
        return None
    if not raw:
        return None
    head, body = raw[0].strip(), [ln for ln in raw[1:] if ln.strip()]
    if head == "sticky":
        return body[:6] or None
    try:
        return (body[:6] or None) if time.time() <= float(head) else None
    except ValueError:
        try:  # legacy header-less file
            if time.time() - os.path.getmtime(MSG_FILE) <= MSG_TTL:
                return [ln for ln in raw if ln.strip()][:6] or None
        except OSError:
            pass
    return None


def render_text(device, lines):
    with canvas(device) as draw:
        y = 0
        for ln in lines[:6]:
            draw.text((0, y), ln[:MAX_COLS], fill="white")
            y += LINE_H


def image_files():
    try:
        return sorted(
            os.path.join(IMAGE_DIR, f) for f in os.listdir(IMAGE_DIR)
            if f.lower().endswith((".png", ".bmp", ".jpg", ".jpeg", ".gif", ".xbm")))
    except OSError:
        return []


def render_image(device, path):
    """Convert any image to 1-bit, fit it centered on the panel, and show it."""
    try:
        img = Image.open(path).convert("L")
    except Exception:
        return False
    img = img.point(lambda p: 255 if p > IMG_THRESHOLD else 0).convert("1")
    img.thumbnail(device.size)  # fit, preserving aspect ratio
    frame = Image.new("1", device.size, 0)
    frame.paste(img, ((device.size[0] - img.size[0]) // 2,
                      (device.size[1] - img.size[1]) // 2))
    name = os.path.basename(path).lower()
    if IMG_INVERT_DEFAULT or "invert" in name or "-inv" in name:
        # Flip the whole frame: white background fills the panel, art goes black.
        frame = ImageOps.invert(frame.convert("L")).convert("1")
    device.display(frame)
    return True


def loop():
    serial = i2c(port=I2C_PORT, address=I2C_ADDR)
    device = ssd1306(serial)            # 128x64
    idx = 0
    while True:
        msg = message_lines()
        if msg:
            render_text(device, msg)
        else:
            pages = [("text", p) for p in standing_pages()]
            pages += [("image", p) for p in image_files()]
            kind, payload = pages[idx % len(pages)]
            if kind == "image" and not render_image(device, payload):
                render_text(device, ["(bad image)", os.path.basename(payload)])
            elif kind == "text":
                render_text(device, payload)
            idx += 1
        time.sleep(PAGE_SECS)


if __name__ == "__main__":
    # Resilient: if the bus is busy (e.g. Argon's OLED daemon still running) or
    # the panel isn't present, log once and keep retrying rather than crash.
    while True:
        try:
            loop()
        except Exception as e:
            print(f"oled: {e}; retrying in 15s "
                  f"(is Argon's OLED daemon disabled? is 0x3c present?)", flush=True)
            time.sleep(15)
