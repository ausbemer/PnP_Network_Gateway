# Traffic generator (no-DHCP test chatter)

A plain bash script — no Docker — that makes a test segment "chatty" enough for
`tailscale-gateway-autonet` to infer it. Run it on any spare Linux box (a Jetson,
a Pi, a mini-PC) wired into the BR2 test VLAN, next to the Pi-under-test.

## What it does

- Claims one or more **static IPs** on the segment (the test VLAN has DHCP
  disabled, so addresses must be static).
- Loops, emitting the two signals autonet relies on:
  - a fresh **ARP for the gateway** every cycle (reveals the subnet + gateway),
  - a **directed broadcast** (reveals the netmask).
- Extra IPs appear as extra hosts to the dashboard's `arp-scan` (they share this
  box's MAC, which is fine for testing).

## Use

```bash
sudo IFACE=eth0 GATEWAY=192.168.77.1 PREFIX=24 \
     IPS="192.168.77.61 192.168.77.62 192.168.77.63" \
     BROADCAST=192.168.77.255 \
     ./traffic-generator.sh
```

Or just edit the defaults at the top and run `sudo ./traffic-generator.sh`.
Match `IFACE` to the box's NIC on the test VLAN, and the addresses to the VLAN
you configured on the BR2 (DHCP off). Ctrl-C stops it and removes the IPs it
added.

## Requirements

- Linux with `iproute2` (`ip`) and `iputils` (`ping` with `-b`) — both standard
  on Jetson/Ubuntu and Raspberry Pi OS.
- Run as root (it configures addresses and routes).

## Notes

- One generator box is enough to exercise autonet's inference. Run it on a second
  box (or add more IPs) for a richer arp-scan result.
- For the IP-conflict test (runbook T3), include the address autonet grabs first
  (e.g. `192.168.77.254`) in `IPS` so it's already taken.
