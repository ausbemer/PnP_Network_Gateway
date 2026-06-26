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
import glob
import ipaddress
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time

from flask import (Flask, abort, jsonify, redirect, render_template_string,
                   request, send_file)
from urllib.parse import quote

# scapy is only needed for the (optional) device-blocking feature. Import it
# defensively so the dashboard still runs if it isn't installed.
try:
    from scapy.all import ARP, Ether, get_if_hwaddr, sendp, srp
    SCAPY_OK = True
except Exception:
    SCAPY_OK = False

app = Flask(__name__)

PORT = int(os.environ.get("DASHBOARD_PORT", "8088"))
TS_IFACE = os.environ.get("TS_IFACE", "tailscale0")
SCAN_TIMEOUT = int(os.environ.get("SCAN_TIMEOUT", "15"))
AUTONET_LOG = os.environ.get("AUTONET_LOG", "/bootfw/autonet.log")
# Archived logs are moved here (kept for the record but NOT shown in the log view).
ARCHIVE_DIR = os.environ.get(
    "AUTONET_ARCHIVE", os.path.join(os.path.dirname(AUTONET_LOG), "autonet-archive"))
# Message file the OLED daemon reads. Dashboard messages are written "sticky"
# (shown until cleared); the tsg-oled CLI writes time-limited ones.
OLED_MSG_FILE = os.environ.get("OLED_MSG_FILE", "/run/tailscale-gateway/oled.msg")
OLED_COLS, OLED_ROWS = 21, 6   # SSD1306 128x64 at the default font
# Root of the file explorer (the NVMe mount, bind-mounted into the container).
FILES_ROOT = os.environ.get("FILES_ROOT", "/data")

# A MAC that (almost certainly) belongs to no one on the segment. Poisoned
# victims send their gateway traffic here, where it goes nowhere — a true
# blackhole. We deliberately do NOT use our own MAC, because this host has IP
# forwarding enabled for Tailscale and would otherwise route the victim's
# traffic for it (defeating the block).
BLACKHOLE_MAC = "02:00:00:00:00:01"

# Active blocks: target_ip -> {"stop": Event, "thread": Thread, "mac": str}.
# Intentionally in-memory only: blocks are manual and ephemeral, and clear on
# restart. No persistence, nothing auto-arms.
_blocks = {}
_blocks_lock = threading.Lock()


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


def iface_subnets(iface):
    """Every global IPv4 the interface holds, as {cidr, my_ip} — one per subnet
    the Pi is multi-homed onto."""
    out = run(["ip", "-4", "-o", "addr", "show", "dev", iface, "scope", "global"])
    subs = []
    for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", out):
        try:
            ifc = ipaddress.ip_interface(m.group(1))
            subs.append({"cidr": str(ifc.network), "my_ip": str(ifc.ip)})
        except ValueError:
            continue
    return subs


def internet_ok():
    """TCP reachability test (not ICMP) so ICMP-filtering firewalls like the
    Siemens Scalance don't show a false 'down'."""
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 443)):
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except OSError:
            continue
    return False


def net_info():
    lan = default_iface()
    gw = default_gateway()
    subs = iface_subnets(lan) if lan else []
    for s in subs:  # mark the subnet that holds the default gateway
        s["is_default"] = False
        if gw:
            try:
                s["is_default"] = ipaddress.ip_address(gw) in ipaddress.ip_network(s["cidr"])
            except ValueError:
                pass
    return {
        "hostname": socket.gethostname(),
        "tailscale_ip": iface_ipv4(TS_IFACE),
        "lan_iface": lan,
        "gateway": gw,
        "internet": internet_ok(),
        "subnets": subs,
    }


def _read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _fmt_uptime(sec):
    d, h, m = int(sec // 86400), int((sec % 86400) // 3600), int((sec % 3600) // 60)
    if d:
        return f"{d}d {h}h"
    return f"{h}h {m}m" if h else f"{m}m"


def cpu_percent(interval=0.25):
    """Whole-system CPU busy %, sampled from /proc/stat over a short interval."""
    def snap():
        with open("/proc/stat") as f:
            v = list(map(int, f.readline().split()[1:]))
        idle = v[3] + (v[4] if len(v) > 4 else 0)
        return idle, sum(v)
    try:
        i1, t1 = snap(); time.sleep(interval); i2, t2 = snap()
        dt = t2 - t1
        return round(100 * (1 - (i2 - i1) / dt)) if dt > 0 else None
    except Exception:
        return None


def sys_stats():
    s = {}
    t = _read_int("/sys/class/thermal/thermal_zone0/temp")
    s["temp_c"] = round(t / 1000, 1) if t is not None else None
    s["fan_rpm"] = None
    for p in (glob.glob("/sys/class/hwmon/hwmon*/fan1_input")
              + glob.glob("/sys/devices/platform/cooling_fan/hwmon/hwmon*/fan1_input")):
        v = _read_int(p)
        if v is not None:
            s["fan_rpm"] = v
            break
    try:
        with open("/proc/loadavg") as f:
            s["load1"] = f.read().split()[0]
    except Exception:
        s["load1"] = None
    s["cores"] = os.cpu_count()
    s["cpu_pct"] = cpu_percent()
    # Memory
    mi = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                mi[k] = int(rest.split()[0]) * 1024   # kB -> bytes
        total = mi["MemTotal"]
        avail = mi.get("MemAvailable", mi.get("MemFree", 0))
        s["mem_used"], s["mem_total"] = total - avail, total
        s["mem_pct"] = round((total - avail) * 100 / total) if total else None
    except Exception:
        s["mem_total"] = None
    # Root disk
    try:
        du = shutil.disk_usage("/")
        s["disk_used"], s["disk_total"] = du.used, du.total
        s["disk_pct"] = round(du.used * 100 / du.total) if du.total else None
    except Exception:
        s["disk_total"] = None
    up = None
    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
    except Exception:
        pass
    s["uptime"] = _fmt_uptime(up) if up is not None else None
    return s


def stats_payload():
    """sys_stats() plus the human-readable size strings the UI shows."""
    s = sys_stats()
    s["mem_str"] = (f"{_hsize(s['mem_used'])} / {_hsize(s['mem_total'])}"
                    if s.get("mem_total") else "—")
    s["disk_str"] = (f"{_hsize(s['disk_used'])} / {_hsize(s['disk_total'])}"
                     if s.get("disk_total") else "—")
    with _loadtest_lock:
        s["loadtest"] = dict(_loadtest)
    return s


def scan_devices(iface, subnets, gateway, my_ips):
    """Active arp-scan of EACH connected subnet. Returns devices tagged with the
    subnet they belong to."""
    if not iface or not subnets:
        return []
    nets = []
    for s in subnets:
        try:
            nets.append(ipaddress.ip_network(s["cidr"]))
        except ValueError:
            pass
    devices = {}
    line_re = re.compile(r"^(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F:]{17})\s*(.*)$")
    for s in subnets:
        out = run(["arp-scan", "--plain", "--interface=" + iface, s["cidr"]],
                  timeout=SCAN_TIMEOUT)
        if not out:  # --plain unsupported on older arp-scan
            out = run(["arp-scan", "--interface=" + iface, s["cidr"]],
                      timeout=SCAN_TIMEOUT)
        for line in out.splitlines():
            m = line_re.match(line.strip())
            if not m:
                continue
            ip, mac, vendor = m.group(1), m.group(2).lower(), m.group(3).strip()
            sub = s["cidr"]
            for n in nets:
                try:
                    if ipaddress.ip_address(ip) in n:
                        sub = str(n)
                        break
                except ValueError:
                    pass
            devices[ip] = {  # dedupe by IP
                "ip": ip, "mac": mac, "vendor": vendor or "—", "subnet": sub,
                "is_gateway": ip == gateway, "is_self": ip in my_ips,
            }
    return sorted(devices.values(),
                  key=lambda d: (d["subnet"], tuple(int(o) for o in d["ip"].split("."))))


# ── Device blocking (ARP blackhole) ───────────────────────────────────────────
def iface_mac(iface):
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            return f.read().strip()
    except Exception:
        return None


def resolve_mac(ip, iface):
    """ARP-resolve an IP to a MAC on the given interface (scapy)."""
    if not (SCAPY_OK and ip and iface):
        return None
    try:
        ans, _ = srp(Ether(dst="ff:ff:ff:ff:ff:ff") / ARP(pdst=ip),
                     timeout=2, iface=iface, verbose=0)
        for _, r in ans:
            return r.hwsrc
    except Exception:
        pass
    return None


def has_ipv6_neighbor(mac):
    """True if this MAC has an IPv6 neighbor entry — a hint the block may not
    fully cut the device, since ARP poisoning only affects IPv4."""
    if not mac:
        return False
    return mac.lower() in run(["ip", "-6", "neigh"]).lower()


def _poison_loop(target_ip, target_mac, gw_ip, gw_mac, iface, stop):
    # Tell the victim the gateway is at the blackhole MAC, and tell the gateway
    # the victim is at the blackhole MAC. Resend until stopped to hold caches.
    to_target = Ether(dst=target_mac) / ARP(
        op=2, psrc=gw_ip, hwsrc=BLACKHOLE_MAC, pdst=target_ip, hwdst=target_mac)
    to_gateway = None
    if gw_mac:
        to_gateway = Ether(dst=gw_mac) / ARP(
            op=2, psrc=target_ip, hwsrc=BLACKHOLE_MAC, pdst=gw_ip, hwdst=gw_mac)
    while not stop.is_set():
        try:
            sendp(to_target, iface=iface, verbose=0)
            if to_gateway is not None:
                sendp(to_gateway, iface=iface, verbose=0)
        except Exception:
            pass
        stop.wait(2)


def _heal(target_ip, target_mac, gw_ip, gw_mac, iface):
    # Re-assert the correct mappings a few times so the device recovers quickly.
    if not (SCAPY_OK and gw_mac):
        return
    pkts = [
        Ether(dst=target_mac) / ARP(op=2, psrc=gw_ip, hwsrc=gw_mac,
                                    pdst=target_ip, hwdst=target_mac),
        Ether(dst=gw_mac) / ARP(op=2, psrc=target_ip, hwsrc=target_mac,
                                pdst=gw_ip, hwdst=gw_mac),
    ]
    for _ in range(5):
        for p in pkts:
            try:
                sendp(p, iface=iface, verbose=0)
            except Exception:
                pass
        time.sleep(0.3)


def start_block(target_ip, target_mac, gw_ip, iface):
    if not SCAPY_OK:
        return False, "packet engine (scapy) not available in this image"
    if not (target_ip and target_mac and iface):
        return False, "missing target/interface info"
    with _blocks_lock:
        if target_ip in _blocks:
            return True, "already blocked"
    gw_mac = resolve_mac(gw_ip, iface) if gw_ip else None
    stop = threading.Event()
    t = threading.Thread(
        target=_poison_loop,
        args=(target_ip, target_mac, gw_ip, gw_mac, iface, stop),
        daemon=True,
    )
    with _blocks_lock:
        _blocks[target_ip] = {"stop": stop, "thread": t, "mac": target_mac,
                              "gw_ip": gw_ip, "gw_mac": gw_mac, "iface": iface}
    t.start()
    return True, "blocking"


def stop_block(target_ip):
    with _blocks_lock:
        info = _blocks.pop(target_ip, None)
    if not info:
        return False, "not blocked"
    info["stop"].set()
    _heal(target_ip, info["mac"], info.get("gw_ip"), info.get("gw_mac"),
          info.get("iface"))
    return True, "unblocked"


def blocked_ips():
    with _blocks_lock:
        return set(_blocks.keys())


# ── Broadcast load-test ramp (bounded test instrument) ────────────────────────
LOADTEST_HARD_CAP = int(os.environ.get("RAMP_HARD_CAP", "200000"))  # absolute pps ceiling
# Per-run CSV logs land on the NVMe (under the file-explorer root) so they persist
# and can be downloaded straight from the dashboard.
LOADTEST_LOG_DIR = os.environ.get("LOADTEST_LOG_DIR", os.path.join(FILES_ROOT, "loadtest"))
DEFAULT_NET_TARGET = os.environ.get("LOADTEST_NET_TARGET", "1.1.1.1")
# Live state for the 1 Hz readout (latest sample). The full time series lives in
# _loadtest_samples and is served to the chart page.
_loadtest = {"running": False, "pps": 0, "achieved": 0, "sent": 0, "max": 0,
             "net_target": DEFAULT_NET_TARGET,
             "gw_avg": None, "gw_jitter": None, "gw_loss": None,
             "net_avg": None, "net_jitter": None, "net_loss": None,
             "csv": None, "started": None, "error": None}
_loadtest_lock = threading.Lock()
_loadtest_stop = threading.Event()
_loadtest_samples = []   # current run's time series (list of dicts), for the chart


def _mac_bytes(mac):
    return bytes(int(x, 16) for x in mac.split(":"))


def _ip_bytes(ip):
    return bytes(int(x) for x in ip.split("."))


def _arp_broadcast(src_mac, src_ip, target_ip):
    eth = b"\xff\xff\xff\xff\xff\xff" + _mac_bytes(src_mac) + struct.pack("!H", 0x0806)
    arp = struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1)
    arp += _mac_bytes(src_mac) + _ip_bytes(src_ip)
    arp += b"\x00" * 6 + _ip_bytes(target_ip)
    frame = eth + arp
    return frame + b"\x00" * (60 - len(frame)) if len(frame) < 60 else frame


def _ping_full(host, secs):
    """Ping `host` once/sec for `secs`; return avg/jitter(mdev)/loss."""
    if not host:
        return {"avg": None, "jitter": None, "loss": None}
    out = run(["ping", "-n", "-c", str(max(1, secs)), "-i", "1", "-W", "1", host],
              timeout=secs + 5)
    loss = re.search(r"(\d+)% packet loss", out)
    rtt = re.search(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", out)  # min/avg/max/mdev
    return {"avg": float(rtt.group(2)) if rtt else None,
            "jitter": float(rtt.group(4)) if rtt else None,
            "loss": int(loss.group(1)) if loss else None}


def _probe_targets(gw, net, secs):
    """Ping both targets concurrently over the same window (same load)."""
    res = {}

    def probe(name, host):
        res[name] = _ping_full(host, secs)

    threads = [threading.Thread(target=probe, args=(n, h))
               for n, h in (("gw", gw), ("net", net))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return res


def _csv_num(v):
    return "" if v is None else v


def _loadtest_run(iface, gw, net, src_mac, src_ip, start, mx, step, step_secs):
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW)
        sock.bind((iface, 0))
    except Exception as e:
        with _loadtest_lock:
            _loadtest.update(running=False, error=str(e))
        return
    frame = _arp_broadcast(src_mac, src_ip, gw or src_ip)

    # Open a per-run CSV on the NVMe. If the mount is missing we still run; the
    # series is kept in memory for the chart regardless.
    csv = None
    csv_path = None
    try:
        os.makedirs(LOADTEST_LOG_DIR, exist_ok=True)
        csv_path = os.path.join(LOADTEST_LOG_DIR, time.strftime("loadtest-%Y%m%d-%H%M%S.csv"))
        csv = open(csv_path, "w")
        csv.write("t_sec,target_pps,achieved_pps,"
                  "gw_avg_ms,gw_jitter_ms,gw_loss_pct,"
                  "net_avg_ms,net_jitter_ms,net_loss_pct\n")
        csv.flush()
        with _loadtest_lock:
            _loadtest["csv"] = csv_path
    except Exception:
        csv = None

    sub = {"pps": 0, "sent": 0}
    sub_stop = threading.Event()

    def sender():
        while not sub_stop.is_set():
            pps = sub["pps"]
            if pps <= 0:
                time.sleep(0.05); continue
            batch = max(1, pps // 100)
            try:
                for _ in range(batch):
                    sock.send(frame); sub["sent"] += 1
            except OSError:
                pass
            with _loadtest_lock:               # publish live so the readout climbs
                _loadtest["sent"] = sub["sent"]
            time.sleep(batch / pps)

    t = threading.Thread(target=sender, daemon=True)
    t.start()
    t0 = time.time()
    try:
        for pps in range(start, mx + 1, step):
            if _loadtest_stop.is_set():
                break
            before = sub["sent"]
            sub["pps"] = pps
            with _loadtest_lock:
                _loadtest.update(pps=pps, sent=sub["sent"])
            res = _probe_targets(gw, net, step_secs)   # blocks ~step_secs under load
            achieved = round((sub["sent"] - before) / max(1, step_secs))
            g, n = res.get("gw", {}), res.get("net", {})
            sample = {
                "t": round(time.time() - t0, 1),
                "target_pps": pps, "achieved_pps": achieved,
                "gw_avg": g.get("avg"), "gw_jitter": g.get("jitter"), "gw_loss": g.get("loss"),
                "net_avg": n.get("avg"), "net_jitter": n.get("jitter"), "net_loss": n.get("loss"),
            }
            _loadtest_samples.append(sample)
            if len(_loadtest_samples) > 5000:
                del _loadtest_samples[0]
            with _loadtest_lock:
                _loadtest.update(
                    pps=pps, achieved=achieved, sent=sub["sent"],
                    gw_avg=g.get("avg"), gw_jitter=g.get("jitter"), gw_loss=g.get("loss"),
                    net_avg=n.get("avg"), net_jitter=n.get("jitter"), net_loss=n.get("loss"))
            if csv:
                csv.write("{t},{tp},{ap},{ga},{gj},{gl},{na},{nj},{nl}\n".format(
                    t=sample["t"], tp=pps, ap=achieved,
                    ga=_csv_num(g.get("avg")), gj=_csv_num(g.get("jitter")), gl=_csv_num(g.get("loss")),
                    na=_csv_num(n.get("avg")), nj=_csv_num(n.get("jitter")), nl=_csv_num(n.get("loss"))))
                csv.flush()
    except Exception as e:
        with _loadtest_lock:
            _loadtest["error"] = str(e)
    finally:
        sub_stop.set()
        try:
            sock.close()
        except Exception:
            pass
        if csv:
            try:
                csv.close()
            except Exception:
                pass
        with _loadtest_lock:
            _loadtest.update(running=False, pps=0)


def start_loadtest(start, mx, step, step_secs, net=DEFAULT_NET_TARGET):
    with _loadtest_lock:
        if _loadtest.get("running"):
            return False, "already running"
    info = net_info()
    iface = info["lan_iface"]
    if not iface:
        return False, "no LAN interface"
    mac, ip = iface_mac(iface), (iface_ipv4(iface) or "0.0.0.0")
    mx = min(mx, LOADTEST_HARD_CAP)
    net = (net or "").strip() or None
    _loadtest_stop.clear()
    _loadtest_samples.clear()
    with _loadtest_lock:
        _loadtest.update(running=True, pps=0, achieved=0, sent=0, max=mx,
                         net_target=net, csv=None,
                         started=time.strftime("%Y-%m-%d %H:%M:%S"),
                         gw_avg=None, gw_jitter=None, gw_loss=None,
                         net_avg=None, net_jitter=None, net_loss=None, error=None)
    threading.Thread(target=_loadtest_run,
                     args=(iface, info["gateway"], net, mac, ip, start, mx, step, step_secs),
                     daemon=True).start()
    return True, "started"


def stop_loadtest():
    _loadtest_stop.set()
    return True, "stopping"


# Standalone chart page (served raw, not Jinja-rendered) — plots the live series.
LOADTEST_CHART_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Load test — chart</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         margin: 0; background: #0f1419; color: #e6edf3; }
  header { padding: 18px 24px; border-bottom: 1px solid #222b34;
           display: flex; justify-content: space-between; align-items: baseline;
           flex-wrap: wrap; gap: 8px; }
  header h1 { font-size: 1.15rem; margin: 0; }
  header a { color: #58a6ff; text-decoration: none; font-size: .85rem; }
  main { padding: 16px 24px 24px; max-width: 1600px; margin: 0 auto; }
  .meta { color: #8b949e; font-size: .85rem; margin-bottom: 12px;
          font-family: ui-monospace, monospace; }
  .meta .run { color: #f85149; } .meta .idle { color: #3fb950; }
  .card { background: #161b22; border: 1px solid #222b34; border-radius: 10px;
          padding: 12px 16px 14px; margin-bottom: 16px; }
  .card h2 { font-size: .95rem; margin: 0 0 8px; color: #c9d1d9; }
  /* Chart fills its box; box height tracks the viewport so both charts share
     the screen and grow/shrink with the window. */
  .cv { position: relative; width: 100%; height: 40vh; min-height: 240px; }
</style></head>
<body>
<header>
  <h1>Network load test — live chart</h1>
  <a href="/">← back to dashboard</a>
</header>
<main>
  <div class="meta" id="meta">loading…</div>
  <div class="card"><h2>Offered vs achieved rate &amp; latency</h2>
    <div class="cv"><canvas id="c1"></canvas></div></div>
  <div class="card"><h2>Packet loss &amp; jitter</h2>
    <div class="cv"><canvas id="c2"></canvas></div></div>
</main>
<script>
(function(){
  var GREEN="#3fb950", BLUE="#58a6ff", AMBER="#d29922", RED="#f85149",
      PURPLE="#bc8cff", CYAN="#39c5cf";
  function ds(label,color,axis,dash){return {label:label,borderColor:color,
    backgroundColor:color,yAxisID:axis,borderWidth:2,pointRadius:0,tension:.25,
    borderDash:dash||[],data:[],spanGaps:true};}
  function axis(id,pos,title,color){return {type:"linear",position:pos,
    title:{display:true,text:title,color:color},
    grid:{color:"rgba(255,255,255,.05)"},ticks:{color:color},
    beginAtZero:true};}
  var common={responsive:true,maintainAspectRatio:false,animation:false,
    interaction:{mode:"index",intersect:false},
    plugins:{legend:{labels:{color:"#c9d1d9",boxWidth:14}}},
    scales:{x:{title:{display:true,text:"seconds",color:"#8b949e"},
      grid:{color:"rgba(255,255,255,.05)"},ticks:{color:"#8b949e",maxTicksLimit:12}}}};

  function clone(o){return JSON.parse(JSON.stringify(o));}
  var o1=clone(common);
  o1.scales.pps=axis("pps","left","pps","#c9d1d9");
  o1.scales.ms=axis("ms","right","latency ms","#8b949e");
  o1.scales.ms.grid={drawOnChartArea:false};
  var c1=new Chart(document.getElementById("c1"),{type:"line",data:{labels:[],datasets:[
    ds("target pps",BLUE,"pps",[6,4]),
    ds("achieved pps",GREEN,"pps"),
    ds("gw latency",AMBER,"ms"),
    ds("net latency",PURPLE,"ms")]},options:o1});

  var o2=clone(common);
  o2.scales.pct=axis("pct","left","loss %","#c9d1d9"); o2.scales.pct.max=100;
  o2.scales.ms=axis("ms","right","jitter ms","#8b949e");
  o2.scales.ms.grid={drawOnChartArea:false};
  var c2=new Chart(document.getElementById("c2"),{type:"line",data:{labels:[],datasets:[
    ds("gw loss %",RED,"pct"),
    ds("net loss %",CYAN,"pct"),
    ds("gw jitter",AMBER,"ms",[4,3]),
    ds("net jitter",PURPLE,"ms",[4,3])]},options:o2});

  async function tick(){
    try{
      var r=await fetch("/api/loadtest/samples",{cache:"no-store"});
      if(!r.ok)return;
      var d=await r.json(), S=d.samples||[];
      var labels=S.map(function(p){return p.t;});
      c1.data.labels=labels;
      c1.data.datasets[0].data=S.map(function(p){return p.target_pps;});
      c1.data.datasets[1].data=S.map(function(p){return p.achieved_pps;});
      c1.data.datasets[2].data=S.map(function(p){return p.gw_avg;});
      c1.data.datasets[3].data=S.map(function(p){return p.net_avg;});
      c1.update();
      c2.data.labels=labels;
      c2.data.datasets[0].data=S.map(function(p){return p.gw_loss;});
      c2.data.datasets[1].data=S.map(function(p){return p.net_loss;});
      c2.data.datasets[2].data=S.map(function(p){return p.gw_jitter;});
      c2.data.datasets[3].data=S.map(function(p){return p.net_jitter;});
      c2.update();
      var m=document.getElementById("meta");
      var state=d.running?'<span class="run">● running</span>':'<span class="idle">○ idle</span>';
      var csv=d.csv?(" · log: "+d.csv.split("/").pop()):"";
      m.innerHTML=state+" · net target "+(d.net_target||"-")+" · max "+(d.max||"-")+
        " pps · "+S.length+" samples"+csv+(d.error?(" · error: "+d.error):"");
    }catch(e){}
  }
  tick(); setInterval(tick,3000);
})();
</script>
</body></html>"""


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
  .tag.blk { background: #f8514933; color: #f85149; }
  tr.blocked td { background: #2a1416; }
  .bar { display: flex; justify-content: space-between; align-items: center;
         margin-bottom: 12px; }
  .bar h2 { font-size: 1rem; margin: 0; }
  .refresh { color: #58a6ff; text-decoration: none; font-size: .9rem; }
  .btn { font: inherit; font-size: .82rem; padding: 4px 12px; border-radius: 6px;
         border: 1px solid transparent; cursor: pointer; }
  .btn.block { background: #b62324; color: #fff; }
  .btn.unblock { background: #21262d; color: #e6edf3; border-color: #3b434b; }
  .warn { color: #d29922; font-size: .72rem; margin-left: 6px; cursor: help; }
  .notice { background: #161b22; border: 1px solid #3b2a12; color: #d29922;
            border-radius: 8px; padding: 8px 12px; font-size: .82rem;
            margin-bottom: 14px; }
  footer { color: #56606a; font-size: .78rem; padding: 0 24px 28px;
           max-width: 980px; margin: 0 auto; }
</style></head>
<body>
<header>
  <h1>{{ info.hostname }}</h1>
  <div>
    <a class="refresh" href="/files" style="margin-right:16px">files →</a>
    <a class="refresh" href="/log" style="margin-right:16px">autonet log →</a>
    <span class="ts">{{ info.tailscale_ip or "tailscale: offline" }}</span>
  </div>
</header>
<main>
  <div class="cards">
    <div class="card"><div class="label">LAN interface</div>
      <div class="value">{{ info.lan_iface or "—" }}</div></div>
    <div class="card"><div class="label">Gateway (default)</div>
      <div class="value">{{ info.gateway or "—" }}</div></div>
    <div class="card"><div class="label">Internet</div>
      <div class="value {{ 'ok' if info.internet else 'bad' }}">
        {{ "reachable" if info.internet else "down" }}</div></div>
    <div class="card"><div class="label">Subnets</div>
      <div class="value">{{ info.subnets|length }}</div></div>
    <div class="card"><div class="label">Tailscale</div>
      <div class="value">{{ info.tailscale_ip or "offline" }}</div></div>
    <div class="card"><div class="label">Devices found</div>
      <div class="value">{{ devices|length }}</div></div>
  </div>

  <div class="bar"><h2>System <span id="st-live" style="color:#3fb950;font-size:.7rem">● live</span></h2></div>
  <div class="cards">
    <div class="card"><div class="label">CPU temp</div>
      <div class="value {% if stats.temp_c is not none and stats.temp_c >= 75 %}bad{% elif stats.temp_c is not none %}ok{% endif %}" id="st-temp">
        {% if stats.temp_c is not none %}{{ stats.temp_c }}°C{% else %}—{% endif %}</div></div>
    <div class="card"><div class="label">Fan</div>
      <div class="value" id="st-fan">{% if stats.fan_rpm is none %}—{% elif stats.fan_rpm == 0 %}off{% else %}{{ stats.fan_rpm }} rpm{% endif %}</div></div>
    <div class="card"><div class="label">CPU</div>
      <div class="value" id="st-cpu">{% if stats.cpu_pct is not none %}{{ stats.cpu_pct }}%{% else %}—{% endif %}{% if stats.load1 %} · ld {{ stats.load1 }}{% endif %}</div></div>
    <div class="card"><div class="label">Memory</div>
      <div class="value" id="st-mem">{{ stats.mem_str }}{% if stats.mem_pct is not none %} · {{ stats.mem_pct }}%{% endif %}</div></div>
    <div class="card"><div class="label">Disk (root)</div>
      <div class="value" id="st-disk">{{ stats.disk_str }}{% if stats.disk_pct is not none %} · {{ stats.disk_pct }}%{% endif %}</div></div>
    <div class="card"><div class="label">Uptime</div>
      <div class="value" id="st-uptime">{{ stats.uptime or "—" }}</div></div>
  </div>

  {% if msg %}<div class="notice">{{ msg }}</div>{% endif %}

  <div class="bar"><h2>OLED message</h2></div>
  <form method="post" action="/oled" style="display:flex; gap:8px; margin-bottom:8px">
    <input name="text" maxlength="120" autocomplete="off"
           placeholder="Send a message to the screen…"
           style="flex:1; background:#161b22; border:1px solid #3b434b; color:#e6edf3;
                  border-radius:6px; padding:6px 10px; font:inherit; font-size:.9rem">
    <button class="btn block">Send</button>
    <button class="btn unblock" formaction="/oled-clear">Clear</button>
  </form>

  <div class="bar"><h2>Network load test <span id="lt-state" style="font-size:.7rem;color:#8b949e"></span></h2>
    <a class="refresh" href="/loadtest/chart" target="_blank" rel="noopener">📈 chart →</a></div>
  <form method="post" action="/loadtest/start"
        onsubmit="return confirm('Start a broadcast load ramp on the LAN? Use only on a network you are authorized to test.')"
        style="display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-bottom:8px">
    <label style="font-size:.82rem; color:#8b949e">start
      <input type="number" name="start" value="100" min="1" style="width:80px"></label>
    <label style="font-size:.82rem; color:#8b949e">max
      <input type="number" name="max" value="1000" min="1" style="width:90px"></label>
    <label style="font-size:.82rem; color:#8b949e">step
      <input type="number" name="step" value="100" min="1" style="width:80px"></label>
    <label style="font-size:.82rem; color:#8b949e">sec/step
      <input type="number" name="step_secs" value="10" min="1" style="width:70px"></label>
    <label style="font-size:.82rem; color:#8b949e">net target
      <input type="text" name="net_target" value="1.1.1.1" style="width:110px"></label>
    <button class="btn block">Start</button>
    <button class="btn unblock" formaction="/loadtest/stop" formnovalidate>Stop</button>
  </form>
  <div class="du" id="lt-live">load test: idle</div>

  <div class="bar">
    <h2>Subnets{% if info.subnets|length > 1 %} · multi-homed across {{ info.subnets|length }}{% endif %}</h2>
  </div>
  <table>
    <thead><tr><th>Subnet</th><th>This device's IP</th><th>Role</th></tr></thead>
    <tbody>
    {% for s in info.subnets %}
      <tr>
        <td class="mac">{{ s.cidr }}</td>
        <td class="mac">{{ s.my_ip }}</td>
        <td>{% if s.is_default %}<span class="tag gw">default · internet</span>
            {% else %}<span class="tag self">advertised only</span>{% endif %}</td>
      </tr>
    {% else %}
      <tr><td colspan="3">No subnets configured.</td></tr>
    {% endfor %}
    </tbody>
  </table>
  {% if not scapy_ok %}<div class="notice">Device blocking is unavailable —
    the packet engine (scapy) isn't installed in this image.</div>{% endif %}

  <div class="bar">
    <h2>Devices across all subnets{% if blocked_count %}
      · <span class="bad">{{ blocked_count }} blocked</span>{% endif %}</h2>
    <a class="refresh" href="/">↻ Rescan</a>
  </div>
  <table>
    <thead><tr><th>IP address</th><th>Subnet</th><th>MAC</th><th>Vendor</th><th>Action</th></tr></thead>
    <tbody>
    {% for d in devices %}
      <tr class="{{ 'blocked' if d.blocked else '' }}">
        <td class="ip"><a href="http://{{ d.ip }}" target="_blank" rel="noopener">{{ d.ip }}</a>
          {% if d.is_gateway %}<span class="tag gw">gateway</span>{% endif %}
          {% if d.is_self %}<span class="tag self">this device</span>{% endif %}
          {% if d.blocked %}<span class="tag blk">blocked</span>{% endif %}
        </td>
        <td class="mac">{{ d.subnet }}</td>
        <td class="mac">{{ d.mac }}</td>
        <td>{{ d.vendor }}</td>
        <td>
          {% if d.is_gateway or d.is_self %}—
          {% elif d.blocked %}
            <form method="post" action="/unblock" style="display:inline">
              <input type="hidden" name="ip" value="{{ d.ip }}">
              <button class="btn unblock">Unblock</button>
            </form>
          {% elif scapy_ok %}
            <form method="post" action="/block" style="display:inline">
              <input type="hidden" name="ip" value="{{ d.ip }}">
              <input type="hidden" name="mac" value="{{ d.mac }}">
              <button class="btn block">Block</button>
            </form>
            {% if d.ipv6 %}<span class="warn"
              title="This device has an IPv6 address. ARP blocking only affects IPv4, so it may stay reachable over IPv6.">v6?</span>{% endif %}
          {% else %}—{% endif %}
        </td>
      </tr>
    {% else %}
      <tr><td colspan="5">No devices found (scan returned nothing).</td></tr>
    {% endfor %}
    </tbody>
  </table>
</main>
<footer>Scanned {{ scanned_at }} · arp-scan on {{ info.lan_iface or "—" }} ·
  served on the tailnet only · blocks are manual and clear on restart</footer>
<script>
(function(){
  var T=function(t){return t==null?"—":t+"°C";};
  var F=function(r){return r==null?"—":(r===0?"off":r+" rpm");};
  var set=function(id,v){var e=document.getElementById(id);if(e)e.textContent=v;};
  async function poll(){
    try{
      var r=await fetch("/api/stats",{cache:"no-store"});
      if(!r.ok)return;
      var s=await r.json();
      var t=document.getElementById("st-temp");
      if(t){t.textContent=T(s.temp_c);
        t.className="value"+(s.temp_c!=null&&s.temp_c>=75?" bad":(s.temp_c!=null?" ok":""));}
      set("st-fan",F(s.fan_rpm));
      set("st-cpu",(s.cpu_pct!=null?s.cpu_pct+"%":"—")+(s.load1?" · ld "+s.load1:""));
      set("st-mem",s.mem_str+(s.mem_pct!=null?" · "+s.mem_pct+"%":""));
      set("st-disk",s.disk_str+(s.disk_pct!=null?" · "+s.disk_pct+"%":""));
      set("st-uptime",s.uptime||"—");
      var lt=s.loadtest||{};
      var ltlive=document.getElementById("lt-live");
      var ms=function(v){return v!=null?v+"ms":"-";};
      var pc=function(v){return v!=null?v+"%":"-";};
      if(ltlive)ltlive.textContent=lt.running
        ?("load test: RUNNING · target "+lt.pps+" / achieved "+(lt.achieved!=null?lt.achieved:"-")
          +" pps · sent "+lt.sent
          +" · gw "+ms(lt.gw_avg)+"/"+pc(lt.gw_loss)+" jit "+ms(lt.gw_jitter)
          +" · net["+(lt.net_target||"-")+"] "+ms(lt.net_avg)+"/"+pc(lt.net_loss)+" jit "+ms(lt.net_jitter)
          +" (max "+lt.max+")")
        :("load test: idle"+(lt.error?(" — error: "+lt.error):""));
      var ltstate=document.getElementById("lt-state");
      if(ltstate){ltstate.textContent=lt.running?"● running":"";ltstate.style.color=lt.running?"#f85149":"#8b949e";}
      var l=document.getElementById("st-live");if(l)l.style.color="#3fb950";
    }catch(e){
      var l=document.getElementById("st-live");if(l)l.style.color="#f85149";
    }
  }
  setInterval(poll,1000); poll();
})();
</script>
</body></html>"""


@app.route("/")
def index():
    info = net_info()
    my_ips = [s["my_ip"] for s in info["subnets"]]
    devices = scan_devices(info["lan_iface"], info["subnets"], info["gateway"], my_ips)
    blocked = blocked_ips()
    for d in devices:
        d["blocked"] = d["ip"] in blocked
        d["ipv6"] = has_ipv6_neighbor(d["mac"])
    scanned_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stats = stats_payload()
    return render_template_string(
        PAGE, info=info, devices=devices, scanned_at=scanned_at,
        scapy_ok=SCAPY_OK, blocked_count=len(blocked), stats=stats,
        msg=request.args.get("msg"))


@app.route("/block", methods=["POST"])
def block():
    info = net_info()
    ip = (request.form.get("ip") or "").strip()
    mac = (request.form.get("mac") or "").strip()
    # Guards: never blackhole the gateway or ourselves.
    if ip and ip == info.get("gateway"):
        return redirect("/?msg=Refused:+that's+the+gateway")
    if ip and ip == info.get("lan_ip"):
        return redirect("/?msg=Refused:+that's+this+device")
    ok, why = start_block(ip, mac, info.get("gateway"), info.get("lan_iface"))
    return redirect(f"/?msg={('Blocking ' + ip) if ok else ('Could not block: ' + why)}")


@app.route("/unblock", methods=["POST"])
def unblock():
    ip = (request.form.get("ip") or "").strip()
    ok, why = stop_block(ip)
    return redirect(f"/?msg={('Unblocked ' + ip) if ok else ('Not blocked: ' + ip)}")


LOG_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>autonet log</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, sans-serif; margin: 0; background: #0f1419; color: #e6edf3; }
  header { padding: 20px 24px; border-bottom: 1px solid #222b34;
           display: flex; justify-content: space-between; align-items: baseline; }
  header h1 { font-size: 1.1rem; margin: 0; }
  a { color: #58a6ff; text-decoration: none; }
  main { padding: 24px; max-width: 980px; margin: 0 auto; }
  pre { background: #161b22; border: 1px solid #222b34; border-radius: 10px;
        padding: 16px; overflow-x: auto; font-family: ui-monospace, monospace;
        font-size: .82rem; line-height: 1.45; white-space: pre-wrap; word-break: break-word; }
  .path { color: #8b949e; font-size: .8rem; margin-bottom: 10px; }
  .btn { font: inherit; font-size: .82rem; padding: 4px 12px; border-radius: 6px;
         border: 1px solid #3b434b; background: #21262d; color: #e6edf3; cursor: pointer; }
  .notice { background: #161b22; border: 1px solid #3b2a12; color: #d29922;
            border-radius: 8px; padding: 8px 12px; font-size: .82rem; margin-bottom: 12px; }
</style></head>
<body>
<header><h1>autonet log</h1>
  <span>
    <form method="post" action="/archive-log" style="display:inline"
          onsubmit="return confirm('Move the current log to the archive folder and clear it?')">
      <button class="btn">Archive log</button>
    </form>
    <a href="/" style="margin-left:14px">← back to status</a>
  </span>
</header>
<main>
  {% if msg %}<div class="notice">{{ msg }}</div>{% endif %}
  <div class="path">{{ path }}{% if archived %} · {{ archived }} archived in {{ archive_dir }}{% endif %}</div>
  <pre>{{ log }}</pre>
</main></body></html>"""


@app.route("/log")
def autonet_log():
    try:
        with open(AUTONET_LOG, "r", errors="replace") as f:
            lines = f.read().splitlines()
        log = "\n".join(lines[-800:]) or "(log file is empty)"
    except FileNotFoundError:
        log = ("(no autonet log yet — autonet runs only when there is no DHCP "
               "lease, or it hasn't run on this boot)")
    except Exception as e:
        log = f"(could not read {AUTONET_LOG}: {e})"
    try:
        archived = len([f for f in os.listdir(ARCHIVE_DIR) if f.endswith(".log")])
    except OSError:
        archived = 0
    return render_template_string(
        LOG_PAGE, log=log, path=AUTONET_LOG, msg=request.args.get("msg"),
        archived=archived, archive_dir=ARCHIVE_DIR)


@app.route("/archive-log", methods=["POST"])
def archive_log():
    """Move the current log to the archive folder and clear the active one. The
    archive is kept on disk but never shown in the log view."""
    try:
        if not (os.path.exists(AUTONET_LOG) and os.path.getsize(AUTONET_LOG) > 0):
            return redirect("/log?msg=Nothing+to+archive")
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(ARCHIVE_DIR, f"autonet-{ts}.log")
        shutil.move(AUTONET_LOG, dest)
        return redirect("/log?msg=Archived+to+" + os.path.basename(dest))
    except Exception as e:
        return redirect("/log?msg=Archive+failed:+" + str(e).replace(" ", "+"))


def oled_wrap(text, width=OLED_COLS, maxlines=OLED_ROWS):
    """Word-wrap text into OLED-sized lines, breaking over-long tokens."""
    out, cur = [], ""
    for w in text.split():
        while len(w) > width:
            if cur:
                out.append(cur); cur = ""
            out.append(w[:width]); w = w[width:]
            if len(out) >= maxlines:
                return out[:maxlines]
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= width:
            cur += " " + w
        else:
            out.append(cur); cur = w
            if len(out) >= maxlines:
                return out[:maxlines]
    if cur and len(out) < maxlines:
        out.append(cur)
    return out[:maxlines]


@app.route("/oled", methods=["POST"])
def oled_send():
    text = (request.form.get("text") or "").strip()
    if not text:
        return redirect("/?msg=Empty+message")
    lines = oled_wrap(text)
    try:
        os.makedirs(os.path.dirname(OLED_MSG_FILE), exist_ok=True)
        # "sticky" header => OLED daemon shows it until cleared.
        with open(OLED_MSG_FILE, "w") as f:
            f.write("sticky\n" + "\n".join(lines) + "\n")
        return redirect("/?msg=Sent+to+OLED")
    except Exception as e:
        return redirect("/?msg=OLED+send+failed:+" + str(e).replace(" ", "+"))


@app.route("/oled-clear", methods=["POST"])
def oled_clear():
    try:
        if os.path.exists(OLED_MSG_FILE):
            os.remove(OLED_MSG_FILE)
        return redirect("/?msg=OLED+cleared")
    except Exception as e:
        return redirect("/?msg=OLED+clear+failed:+" + str(e).replace(" ", "+"))


# ── File explorer (scoped to the NVMe mount) ──────────────────────────────────
def _safe(rel):
    """Resolve a relative path under FILES_ROOT, rejecting any escape (../,
    absolute paths, symlinks pointing out). Returns abs path or None."""
    base = os.path.realpath(FILES_ROOT)
    full = os.path.realpath(os.path.join(base, (rel or "").lstrip("/")))
    if full == base or full.startswith(base + os.sep):
        return full
    return None


def _hsize(n):
    if n is None:
        return ""
    n = float(n)
    for u in ("B", "K", "M", "G", "T"):
        if n < 1024:
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}P"


def _relto_root(full):
    base = os.path.realpath(FILES_ROOT)
    rel = os.path.relpath(full, base)
    return "" if rel == "." else rel


FILES_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Files</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, sans-serif; margin: 0; background: #0f1419; color: #e6edf3; }
  header { padding: 18px 24px; border-bottom: 1px solid #222b34;
           display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 8px; }
  header h1 { font-size: 1.1rem; margin: 0; }
  a { color: #58a6ff; text-decoration: none; }
  main { padding: 20px 24px; max-width: 980px; margin: 0 auto; }
  .du { color: #8b949e; font-size: .82rem; margin-bottom: 12px; }
  .crumbs { margin-bottom: 12px; font-family: ui-monospace, monospace; font-size: .9rem; }
  table { width: 100%; border-collapse: collapse; }
  th,td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #222b34; font-size: .9rem; }
  th { color: #8b949e; font-size: .72rem; text-transform: uppercase; letter-spacing: .05em; }
  td.sz { font-family: ui-monospace, monospace; color: #8b949e; text-align: right; white-space: nowrap; }
  .btn { font: inherit; font-size: .8rem; padding: 3px 10px; border-radius: 6px; cursor: pointer;
         border: 1px solid #3b434b; background: #21262d; color: #e6edf3; }
  .btn.del { background: #b62324; border-color: #b62324; color: #fff; }
  form.inline { display: inline; }
  .bar { display: flex; gap: 8px; flex-wrap: wrap; margin: 18px 0; align-items: center; }
  input[type=text] { background: #161b22; border: 1px solid #3b434b; color: #e6edf3;
                     border-radius: 6px; padding: 6px 10px; font: inherit; }
  .notice { background: #161b22; border: 1px solid #3b2a12; color: #d29922;
            border-radius: 8px; padding: 8px 12px; font-size: .82rem; margin-bottom: 12px; }
</style></head>
<body>
<header><h1>Files</h1><a href="/">← back to status</a></header>
<main>
  {% if msg %}<div class="notice">{{ msg }}</div>{% endif %}
  {% if disk %}<div class="du">Storage: {{ disk.used }} used of {{ disk.total }}
    · {{ disk.free }} free</div>{% endif %}

  <div class="crumbs">
    <a href="/files">root</a>{% for c in crumbs %} / <a href="/files?path={{ c.rel|urlencode }}">{{ c.name }}</a>{% endfor %}
  </div>

  <table>
    <thead><tr><th>Name</th><th class="sz">Size</th><th>Actions</th></tr></thead>
    <tbody>
    {% if parent is not none %}
      <tr><td colspan="3"><a href="/files?path={{ parent|urlencode }}">.. (up)</a></td></tr>
    {% endif %}
    {% for e in entries %}
      <tr>
        <td>{% if e.isdir %}📁 <a href="/files?path={{ e.rel|urlencode }}">{{ e.name }}/</a>
            {% else %}📄 {{ e.name }}{% endif %}</td>
        <td class="sz">{{ e.size }}</td>
        <td>
          {% if not e.isdir %}<a class="btn" href="/files/download?path={{ e.rel|urlencode }}">Download</a>{% endif %}
          <form class="inline" method="post" action="/files/delete"
                onsubmit="return confirm('Delete {{ e.name }}?')">
            <input type="hidden" name="path" value="{{ e.rel }}">
            <button class="btn del">Delete</button>
          </form>
        </td>
      </tr>
    {% else %}
      <tr><td colspan="3">(empty)</td></tr>
    {% endfor %}
    </tbody>
  </table>

  <div class="bar">
    <form method="post" action="/files/upload" enctype="multipart/form-data" style="display:flex; gap:8px">
      <input type="hidden" name="path" value="{{ cur }}">
      <input type="file" name="file">
      <button class="btn">Upload here</button>
    </form>
    <form method="post" action="/files/mkdir" style="display:flex; gap:8px">
      <input type="hidden" name="path" value="{{ cur }}">
      <input type="text" name="name" placeholder="new folder" maxlength="64">
      <button class="btn">Create folder</button>
    </form>
  </div>
</main></body></html>"""


@app.route("/files")
def files():
    full = _safe(request.args.get("path", ""))
    if not full or not os.path.isdir(full):
        return redirect("/files?msg=" + quote("No storage mounted, or not a folder"))
    cur = _relto_root(full)
    entries = []
    try:
        names = os.listdir(full)
    except OSError:
        names = []
    for name in names:
        p = os.path.join(full, name)
        isdir = os.path.isdir(p)
        try:
            size = None if isdir else os.path.getsize(p)
        except OSError:
            size = None
        entries.append({"name": name, "isdir": isdir,
                        "size": "" if isdir else _hsize(size),
                        "rel": _relto_root(p)})
    entries.sort(key=lambda e: (not e["isdir"], e["name"].lower()))
    parent = None if not cur else (os.path.dirname(cur) if os.path.dirname(cur) else "")
    if cur and parent == "":
        parent = ""  # up to root
    if not cur:
        parent = None
    crumbs, acc = [], ""
    for part in [p for p in cur.split("/") if p]:
        acc = (acc + "/" + part) if acc else part
        crumbs.append({"name": part, "rel": acc})
    disk = None
    try:
        u = shutil.disk_usage(full)
        disk = {"used": _hsize(u.used), "total": _hsize(u.total), "free": _hsize(u.free)}
    except Exception:
        pass
    return render_template_string(FILES_PAGE, entries=entries, cur=cur, parent=parent,
                                  crumbs=crumbs, disk=disk, msg=request.args.get("msg"))


@app.route("/files/download")
def files_download():
    full = _safe(request.args.get("path", ""))
    if not full or not os.path.isfile(full):
        abort(404)
    return send_file(full, as_attachment=True)


@app.route("/files/upload", methods=["POST"])
def files_upload():
    cur = request.form.get("path", "")
    dest = _safe(cur)
    if not dest or not os.path.isdir(dest):
        abort(403)
    f = request.files.get("file")
    if not f or not f.filename:
        return redirect("/files?path=" + quote(cur) + "&msg=" + quote("No file selected"))
    name = os.path.basename(f.filename)
    try:
        f.save(os.path.join(dest, name))
        m = "Uploaded " + name
    except Exception as e:
        m = "Upload failed: " + str(e)
    return redirect("/files?path=" + quote(cur) + "&msg=" + quote(m))


@app.route("/files/delete", methods=["POST"])
def files_delete():
    full = _safe(request.form.get("path", ""))
    if not full or full == os.path.realpath(FILES_ROOT):
        abort(403)
    parent = os.path.dirname(_relto_root(full))
    try:
        if os.path.isdir(full):
            shutil.rmtree(full)
        else:
            os.remove(full)
    except Exception:
        pass
    return redirect("/files?path=" + quote(parent))


@app.route("/files/mkdir", methods=["POST"])
def files_mkdir():
    cur = request.form.get("path", "")
    base = _safe(cur)
    name = os.path.basename((request.form.get("name") or "").strip())
    if base and name:
        try:
            os.makedirs(os.path.join(base, name), exist_ok=True)
        except Exception:
            pass
    return redirect("/files?path=" + quote(cur))


@app.route("/loadtest/start", methods=["POST"])
def loadtest_start():
    try:
        s = max(1, int(request.form.get("start", 100)))
        m = max(1, int(request.form.get("max", 1000)))
        st = max(1, int(request.form.get("step", 100)))
        secs = max(1, int(request.form.get("step_secs", 10)))
    except ValueError:
        return redirect("/?msg=Bad+load-test+params")
    net = request.form.get("net_target", DEFAULT_NET_TARGET)
    ok, why = start_loadtest(s, m, st, secs, net=net)
    return redirect("/?msg=" + quote("Load test " + ("started" if ok else why)))


@app.route("/loadtest/stop", methods=["POST"])
def loadtest_stop():
    stop_loadtest()
    return redirect("/?msg=Load+test+stopping")


@app.route("/api/loadtest/samples")
def api_loadtest_samples():
    """Current run's full time series, for the chart page."""
    with _loadtest_lock:
        meta = {k: _loadtest.get(k) for k in
                ("running", "max", "net_target", "csv", "started", "error")}
    meta["samples"] = list(_loadtest_samples)
    return jsonify(meta)


@app.route("/loadtest/chart")
def loadtest_chart():
    return LOADTEST_CHART_PAGE


@app.route("/api/stats")
def api_stats():
    """Fast-changing system stats for the 1 Hz dashboard poll (no arp-scan)."""
    return jsonify(stats_payload())


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
