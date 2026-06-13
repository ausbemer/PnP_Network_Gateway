# Autonet test runbook

A repeatable protocol for validating the gateway's no-DHCP auto-configuration
(`tailscale-gateway-autonet`) and the surrounding stack, using the BR2 test VLAN
and the [traffic generator](../traffic-generator/) running on a spare Linux box
(e.g. a Jetson). Each test has explicit pass/fail criteria so results are
comparable across code/firmware versions.

Record outcomes in the [results table](#results-log) at the bottom.

---

## 0. Test bed setup

### Hardware
- **Peplink BR2** with cellular WAN (provides real internet upstream).
- **Linux box** (e.g. a Jetson) wired into the test VLAN, running the traffic
  generator.
- **Pi under test**, flashed with the gateway image.
- **Serial console** to the Pi (USB-TTL on the UART, `enable_uart=1`). This is
  the source of truth: on a no-DHCP segment the Pi may not be on the network or
  tailnet, so you cannot rely on SSH.
- **A remote Tailscale node** (phone on cellular, or a node on another network)
  to test reachability into the routed subnet.

### BR2 configuration (do once)
1. Create a dedicated **VLAN** for testing (keeps it off your production LAN).
2. Set the VLAN network to `192.168.77.0/24`, gateway `192.168.77.1`.
3. **Disable the DHCP server** on that VLAN.
4. Confirm the cellular WAN is up and the VLAN has internet (plug in a
   statically-addressed laptop and ping `1.1.1.1`).

### Traffic generator (do before each "chatty" test)
On the Linux box wired to the test VLAN:
```bash
cd Test-Harness/traffic-generator
sudo IFACE=eth0 GATEWAY=192.168.77.1 PREFIX=24 \
     IPS="192.168.77.61 192.168.77.62 192.168.77.63" \
     BROADCAST=192.168.77.255 \
     ./traffic-generator.sh
```
Leave it running; it prints each cycle. Ctrl-C stops it and removes the IPs.

### Watching the Pi (no serial cable needed)

`autonet` writes a copy of every run to **`autonet.log` on the boot (FAT)
partition**, so you have two cable-free ways to read it:

- **Success case** — once the Pi is on the tailnet, open the dashboard and click
  **autonet log →** (or browse to `http://<pi-tailscale-ip>:8088/log`).
- **Failure case** — power off, pull the SD card, put it in any computer, and
  open `autonet.log` on the boot partition (the FAT volume that mounts
  automatically on Windows/macOS).

If you do have a console (serial header or HDMI), the same output is live there
and in the journal:
```bash
journalctl -u tailscale-gateway-autonet -b
journalctl -u tailscale-gateway -b
ip addr ; ip route
```

> **Tip:** for the very first run on new hardware, do a dry pass so the Pi
> observes and reports without reconfiguring itself:
> ```bash
> sudo /usr/local/bin/tailscale-gateway-autonet.sh --dry-run
> ```
> It logs the inferred subnet/gateway and the address it *would* claim, changing
> nothing. (It still sends ARP conflict-detection probes, but sets no IP/route.)

---

## 1. Tests

### T1 — DHCP present (regression guard)
**Goal:** autonet must defer when DHCP works.
**Setup:** temporarily *re-enable* DHCP on the BR2 test VLAN.
**Steps:** boot the Pi; watch the autonet log.
**PASS:** log shows `DHCP succeeded` (or `default route already present`); the Pi
gets a DHCP address; autonet makes **no** static changes.
**FAIL:** autonet enters auto-static mode, or overrides the DHCP lease.
*(Re-disable DHCP before the remaining tests.)*

### T2 — No DHCP, chatty /24 (happy path)
**Goal:** infer the network and self-configure.
**Setup:** DHCP off; traffic generator running (IPs .61–.63 on the VLAN).
**Steps:** boot the Pi; watch the log through to completion.
**PASS, all of:**
- `no DHCP on <iface>; entering auto-static mode`
- `inferred subnet 192.168.77.0/24, gateway 192.168.77.1`
- `verified internet via 192.168.77.1`
- `auto-static configuration complete: 192.168.77.<free>/24 via 192.168.77.1`
- `ip route` shows a default via `.1`; `ping 1.1.1.1` works.
**FAIL:** wrong subnet/gateway, no internet, or service ends `failed`.

### T3 — IP conflict / ACD
**Goal:** never claim an address already in use.
**Setup:** make the generator occupy the address autonet grabs first (it scans
high→low, so `.254`): add it to `IPS`, e.g.
`IPS="192.168.77.61 192.168.77.62 192.168.77.254"`, and (re)start the generator.
**Steps:** boot the Pi.
**PASS:** the `auto-static configuration complete` line shows an address **other
than `.254`** (e.g. `.253`), and `.254` stays reachable.
**FAIL:** autonet selects `.254`, or `.254` starts flapping.
> Note: autonet logs only the address it *chooses*; skipped/in-use candidates
> are silent. The proof is that the chosen address avoids the occupied one.

### T4 — Netmask inference (/23)
**Goal:** derive the prefix from a directed broadcast, not just default /24.
**Setup:** set the BR2 VLAN to `192.168.76.0/23`, gateway `192.168.76.1`. Run the
generator with `GATEWAY=192.168.76.1 PREFIX=23 BROADCAST=192.168.77.255` and
`IPS` in the `.76`/`.77` range.
**Steps:** boot the Pi.
**PASS:** log shows `inferred subnet 192.168.76.0/23` (prefix **23**, not 24).
**FAIL:** prefix inferred as `/24` despite the `/23` directed broadcast.

### T5 — Silent segment (graceful failure)
**Goal:** fail cleanly when there's nothing to infer from.
**Setup:** DHCP off; stop the generator (Ctrl-C, no chatter); Pi alone with the BR2.
**Steps:** boot the Pi.
**PASS:** log shows `captured no traffic; cannot infer network` (or `no host
addresses observed`); the service ends `failed`; **no** bogus IP/route applied.
**FAIL:** autonet applies a guessed address, or hangs past its timeout.

### T6 — Hot-swap DHCP → static
**Goal:** the watcher triggers autonet when moved onto a no-DHCP net.
**Setup:** boot the Pi on a normal DHCP network and let it come up fully. Have
the traffic generator running on the static VLAN.
**Steps:** move the Pi's cable to the static test VLAN (don't reboot). Watch
`journalctl -u tailscale-gateway-watch -f`.
**PASS:** watch log shows `no default route — attempting auto-network
configuration`; autonet runs; the Pi self-configures on `.77`; the gateway
restarts and re-advertises the new subnet.
**FAIL:** the Pi stays unconfigured after the swap.

### T7 — End-to-end reachability + return path (SNAT)
**Goal:** confirm a remote tailnet node can actually reach LAN hosts.
**Setup:** T2 passing; route approved in Tailscale (autoApprovers or manual).
**Steps:** from the remote tailnet node, `ping 192.168.77.61` (a generator IP) and
open `http://192.168.77.61` if it serves anything.
**PASS:** replies come back. On the Pi, `tcpdump -ni <lan> icmp` shows the
forwarded echo leaving with **source = the Pi's LAN IP** (SNAT applied).
**FAIL:** no replies, or tcpdump shows source still `100.64.x.x` (SNAT missing).

### T8 — Dashboard
**Goal:** the status UI is reachable and correct over the tailnet only.
**Steps:** from a tailnet node, open `http://<pi-tailscale-ip>:8088`.
**PASS:** page loads showing the inferred subnet/gateway and lists the generator IPs
with clickable links; the same URL on the **LAN IP** is **refused** (tailnet-only
binding).
**FAIL:** page reachable on the LAN IP, or data is wrong/empty.

---

## 2. Results log

| Date | Code rev | BR2 fw | T1 | T2 | T3 | T4 | T5 | T6 | T7 | T8 | Notes |
|------|----------|--------|----|----|----|----|----|----|----|----|-------|
|      |          |        |    |    |    |    |    |    |    |    |       |
|      |          |        |    |    |    |    |    |    |    |    |       |

Mark each P (pass) / F (fail) / – (skipped). File an issue for any F with the
relevant `journalctl` excerpt.
