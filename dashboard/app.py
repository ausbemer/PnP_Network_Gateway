#!/usr/bin/env python3
"""
Tailscale Gateway dashboard.

A small read-only status page for the gateway Pi. It shows where the device
landed on the network (interface, subnet, gateway, internet reachability, the
Tailscale address) and the live devices on the local subnet, each linked to its
own web UI so you can jump straight to it over the tailnet.

Security model: this binds ONLY to the Pi's Tailscale interface address, so it
is reachable solely by members of your tailnet. There is intentionally no
password — the tailnet membership is the trust boundary. Do not change the bind
address to 0.0.0.0 without adding authentication first.
"""
import datetime
import os
import re
import socket
import subprocess
import time

from flask import Flask, render_template_string

app = Flask(__name__)

PORT = int(os.environ.get("DASHBOARD_PORT", "8088"))
TS_IFACE = os.environ.get("TS_IFACE", "tailscale0")
SCAN_TIMEOUT = int(os.environ.get("SCAN_TIMEOUT", "15"))


def run(cmd, timeout=10):
    """Run a command, returning stdout (str) or '' on any failure."""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return out.stdout
    except Exception:
        return ""


def iface_ipv4(iface):
    """First global IPv4 address on an interface, or None."""
    out = run(["ip", "-4", "-o", "addr", "show", "dev", iface])
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


def default_iface():
    out = run(["ip", "route", "show", "default"])
    m = re.search(r"default via \S+ dev (\S+)", out)
    return m.group(1) if m else None


def default_gateway():
    out = run(["ip", "route", "show", "default"])
    m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


def iface_subnet(iface):
    out = run(["ip", "route", "show", "dev", iface, "proto", "kernel"])
    m = re.search(r"(\d+\.\d+\.\d+\.\d+/\d+)", out)
    return m.group(1) if m else None


def internet_ok():
    out = subprocess.run(
        ["ping", "-c", "1", "-W", "2", "1.1.1.1"],
        capture_output=True, check=False,
    )
    return out.returncode == 0


def net_info():
    lan = default_iface()
    return {
        "hostname": socket.gethostname(),
        "tailscale_ip": iface_ipv4(TS_IFACE),
        "lan_iface": lan,
        "lan_ip": iface_ipv4(lan) if lan else None,
        "subnet": iface_subnet(lan) if lan else None,
        "gateway": default_gateway(),
        "internet": internet_ok(),
    }


def scan_devices(iface, gateway, self_ip):
    """Active arp-scan of the local subnet. Returns a sorted list of dicts."""
    if not iface:
        return []
    out = run(
        ["arp-scan", "--localnet", "--plain", "--interface=" + iface],
        timeout=SCAN_TIMEOUT,
    )
    if not out:  # --plain may be unsupported on older arp-scan; retry plain-ish
        out = run(["arp-scan", "--localnet", "--interface=" + iface],
                  timeout=SCAN_TIMEOUT)
    devices = {}
    line_re = re.compile(
        r"^(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F:]{17})\s*(.*)$"
    )
    for line in out.splitlines():
        m = line_re.match(line.strip())
        if not m:
            continue
        ip, mac, vendor = m.group(1), m.group(2).lower(), m.group(3).strip()
        devices[ip] = {  # dedupe by IP; arp-scan can list duplicates
            "ip": ip,
            "mac": mac,
            "vendor": vendor or "—",
            "is_gateway": ip == gateway,
            "is_self": ip == self_ip,
        }
    return sorted(devices.values(), key=lambda d: tuple(int(o) for o in d["ip"].split(".")))


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ info.hostname }} — Gateway</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         margin: 0; background: #0f1419; color: #e6edf3; }
  header { padding: 20px 24px; border-bottom: 1px solid #222b34;
           display: flex; justify-content: space-between; align-items: baseline;
           flex-wrap: wrap; gap: 8px; }
  header h1 { font-size: 1.3rem; margin: 0; }
  header .ts { color: #58a6ff; font-family: ui-monospace, monospace; }
  main { padding: 24px; max-width: 980px; margin: 0 auto; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr));
           gap: 14px; margin-bottom: 28px; }
  .card { background: #161b22; border: 1px solid #222b34; border-radius: 10px;
          padding: 14px 16px; }
  .card .label { font-size: .72rem; text-transform: uppercase;
                 letter-spacing: .06em; color: #8b949e; }
  .card .value { font-size: 1.15rem; margin-top: 4px;
                 font-family: ui-monospace, monospace; word-break: break-all; }
  .ok { color: #3fb950; } .bad { color: #f85149; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid #222b34;
           font-size: .92rem; }
  th { color: #8b949e; font-weight: 600; font-size: .75rem;
       text-transform: uppercase; letter-spacing: .05em; }
  td.ip a { color: #58a6ff; text-decoration: none; font-family: ui-monospace, monospace; }
  td.ip a:hover { text-decoration: underline; }
  .mac { font-family: ui-monospace, monospace; color: #8b949e; }
  .tag { font-size: .68rem; padding: 1px 7px; border-radius: 999px;
         margin-left: 6px; vertical-align: middle; }
  .tag.gw { background: #1f6feb33; color: #58a6ff; }
  .tag.self { background: #23863633; color: #3fb950; }
  .bar { display: flex; justify-content: space-between; align-items: center;
         margin-bottom: 12px; }
  .bar h2 { font-size: 1rem; margin: 0; }
  .refresh { color: #58a6ff; text-decoration: none; font-size: .9rem; }
  footer { color: #56606a; font-size: .78rem; padding: 0 24px 28px;
           max-width: 980px; margin: 0 auto; }
</style></head>
<body>
<header>
  <h1>{{ info.hostname }}</h1>
  <div class="ts">{{ info.tailscale_ip or "tailscale: offline" }}</div>
</header>
<main>
  <div class="cards">
    <div class="card"><div class="label">LAN interface</div>
      <div class="value">{{ info.lan_iface or "—" }}</div></div>
    <div class="card"><div class="label">This device</div>
      <div class="value">{{ info.lan_ip or "—" }}</div></div>
    <div class="card"><div class="label">Subnet</div>
      <div class="value">{{ info.subnet or "—" }}</div></div>
    <div class="card"><div class="label">Gateway</div>
      <div class="value">{{ info.gateway or "—" }}</div></div>
    <div class="card"><div class="label">Internet</div>
      <div class="value {{ 'ok' if info.internet else 'bad' }}">
        {{ "reachable" if info.internet else "down" }}</div></div>
    <div class="card"><div class="label">Devices found</div>
      <div class="value">{{ devices|length }}</div></div>
  </div>

  <div class="bar">
    <h2>Devices on {{ info.subnet or "subnet" }}</h2>
    <a class="refresh" href="/">↻ Rescan</a>
  </div>
  <table>
    <thead><tr><th>IP address</th><th>MAC</th><th>Vendor</th></tr></thead>
    <tbody>
    {% for d in devices %}
      <tr>
        <td class="ip"><a href="http://{{ d.ip }}" target="_blank" rel="noopener">{{ d.ip }}</a>
          {% if d.is_gateway %}<span class="tag gw">gateway</span>{% endif %}
          {% if d.is_self %}<span class="tag self">this device</span>{% endif %}
        </td>
        <td class="mac">{{ d.mac }}</td>
        <td>{{ d.vendor }}</td>
      </tr>
    {% else %}
      <tr><td colspan="3">No devices found (scan returned nothing).</td></tr>
    {% endfor %}
    </tbody>
  </table>
</main>
<footer>Scanned {{ scanned_at }} · arp-scan on {{ info.lan_iface or "—" }} ·
  served on the tailnet only</footer>
</body></html>"""


@app.route("/")
def index():
    info = net_info()
    devices = scan_devices(info["lan_iface"], info["gateway"], info["lan_ip"])
    scanned_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return render_template_string(PAGE, info=info, devices=devices,
                                  scanned_at=scanned_at)


@app.route("/healthz")
def healthz():
    return "ok\n"


def wait_for_bind_ip():
    """Block until the Tailscale interface has an IP, then return it."""
    while True:
        ip = iface_ipv4(TS_IFACE)
        if ip:
            return ip
        print(f"dashboard: waiting for {TS_IFACE} to come up...", flush=True)
        time.sleep(3)


if __name__ == "__main__":
    bind_ip = wait_for_bind_ip()
    print(f"dashboard: binding to {bind_ip}:{PORT} ({TS_IFACE})", flush=True)
    app.run(host=bind_ip, port=PORT)
