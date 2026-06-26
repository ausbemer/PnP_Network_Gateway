#!/usr/bin/env python3
"""
Controlled broadcast load ramp — a network TEST instrument.

Sends broadcast ARP frames at a steadily increasing, rate-limited pace and, at
each step, measures the network's reaction (ping latency + packet loss to the
gateway). The point is to find *if and when* your switch/equipment starts to
struggle — i.e. where storm-control or CPU limits kick in — by watching the
metrics climb as the rate ramps.

This is a bounded, measured ramp that reports every rate it uses. It is NOT a
flood tool, and it has a hard pps ceiling. Use ONLY on networks you own or are
explicitly authorized to test.

Run as root (needs a raw socket). Ctrl-C stops cleanly at any point.
"""
import argparse
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time

# Absolute ceiling so this can't be turned into an unbounded flood. Override with
# RAMP_HARD_CAP if you genuinely need higher on your own lab gear.
HARD_CAP_PPS = int(os.environ.get("RAMP_HARD_CAP", "20000"))


def _run(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def default_iface():
    m = re.search(r"dev (\S+)", _run(["ip", "route", "show", "default"]))
    return m.group(1) if m else "eth0"


def default_gw():
    m = re.search(r"via (\d+\.\d+\.\d+\.\d+)", _run(["ip", "route", "show", "default"]))
    return m.group(1) if m else ""


def iface_mac(iface):
    with open(f"/sys/class/net/{iface}/address") as f:
        return f.read().strip()


def iface_ip(iface):
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", _run(["ip", "-4", "-o", "addr", "show", "dev", iface]))
    return m.group(1) if m else "0.0.0.0"


def _mac_bytes(mac):
    return bytes(int(x, 16) for x in mac.split(":"))


def _ip_bytes(ip):
    return bytes(int(x) for x in ip.split("."))


def build_arp_broadcast(src_mac, src_ip, target_ip):
    """A broadcast ARP 'who-has' frame (padded to the 60-byte Ethernet minimum)."""
    eth = b"\xff\xff\xff\xff\xff\xff" + _mac_bytes(src_mac) + struct.pack("!H", 0x0806)
    arp = struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1)          # htype ptype hlen plen op=request
    arp += _mac_bytes(src_mac) + _ip_bytes(src_ip)           # sender
    arp += b"\x00" * 6 + _ip_bytes(target_ip)                # target
    frame = eth + arp
    if len(frame) < 60:
        frame += b"\x00" * (60 - len(frame))
    return frame


class Sender(threading.Thread):
    """Sends `frame` at `self.pps` (0 = idle). Set .pps to change the rate live."""
    def __init__(self, iface, frame):
        super().__init__(daemon=True)
        self.frame = frame
        self.pps = 0
        self.sent = 0
        self.stop = threading.Event()
        self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
        self.sock.bind((iface, 0))

    def run(self):
        while not self.stop.is_set():
            pps = self.pps
            if pps <= 0:
                time.sleep(0.05)
                continue
            batch = max(1, pps // 100)        # send in small bursts ~100x/sec
            interval = batch / pps
            try:
                for _ in range(batch):
                    self.sock.send(self.frame)
                    self.sent += 1
            except OSError:
                pass
            time.sleep(interval)


def ping_stats(gw, secs):
    """Ping the gateway once/sec for `secs`; return (avg_ms, max_ms, loss_pct)."""
    out = _run(["ping", "-n", "-c", str(max(1, secs)), "-i", "1", "-W", "1", gw],
               timeout=secs + 5)
    loss = re.search(r"(\d+)% packet loss", out)
    rtt = re.search(r"=\s*[\d.]+/([\d.]+)/([\d.]+)", out)
    return (float(rtt.group(1)) if rtt else None,
            float(rtt.group(2)) if rtt else None,
            int(loss.group(1)) if loss else None)


def _fmt(v):
    return "-" if v is None else (f"{v:.1f}" if isinstance(v, float) else str(v))


def main():
    ap = argparse.ArgumentParser(description="Controlled broadcast load ramp (test instrument).")
    ap.add_argument("--iface", default=None, help="interface (default: default-route iface)")
    ap.add_argument("--gateway", default=None, help="IP to ping for reaction metrics (default: default gw)")
    ap.add_argument("--start", type=int, default=50, help="starting packets/sec")
    ap.add_argument("--max", type=int, default=1000, help="max packets/sec")
    ap.add_argument("--step", type=int, default=50, help="pps increase per step")
    ap.add_argument("--step-secs", type=int, default=10, help="seconds to hold + measure each step")
    ap.add_argument("--csv", default=None, help="write per-step metrics to this CSV")
    ap.add_argument("--dry-run", action="store_true", help="print the ramp plan, send nothing")
    a = ap.parse_args()

    iface = a.iface or default_iface()
    gw = a.gateway or default_gw()
    mx = min(a.max, HARD_CAP_PPS)
    if a.max > HARD_CAP_PPS:
        print(f"NOTE: capping max to the hard ceiling of {HARD_CAP_PPS} pps.")

    if not a.dry_run and os.geteuid() != 0:
        print("Run as root (needs a raw socket).", file=sys.stderr)
        sys.exit(1)

    print(f"iface={iface}  gateway={gw or 'none'}  ramp {a.start}->{mx} pps  "
          f"step +{a.step}/{a.step_secs}s  (Ctrl-C to stop)")
    print(f"{'pps':>7} {'sent':>9} {'ping_avg_ms':>12} {'ping_max_ms':>12} {'loss%':>6}")

    csv = open(a.csv, "w") if a.csv else None
    if csv:
        csv.write("pps,sent,ping_avg_ms,ping_max_ms,loss_pct\n")

    sender = None
    if not a.dry_run:
        frame = build_arp_broadcast(iface_mac(iface), iface_ip(iface), gw or iface_ip(iface))
        sender = Sender(iface, frame)
        sender.start()

    try:
        for pps in range(a.start, mx + 1, a.step):
            if a.dry_run:
                print(f"{pps:>7} {'(dry)':>9} {'-':>12} {'-':>12} {'-':>6}")
                time.sleep(0.1)
                continue
            before = sender.sent
            sender.pps = pps
            if gw:
                avg, mxr, loss = ping_stats(gw, a.step_secs)   # blocks ~step_secs under load
            else:
                time.sleep(a.step_secs)
                avg = mxr = loss = None
            sent = sender.sent - before
            print(f"{pps:>7} {sent:>9} {_fmt(avg):>12} {_fmt(mxr):>12} {_fmt(loss):>6}")
            if csv:
                csv.write(f"{pps},{sent},{avg or ''},{mxr or ''},{loss or ''}\n")
                csv.flush()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        if sender:
            sender.pps = 0
            sender.stop.set()
        if csv:
            csv.close()


if __name__ == "__main__":
    main()
