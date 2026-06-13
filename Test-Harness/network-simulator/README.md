# Network simulator (autonet test segment)

Spins up several fake LAN hosts on a single edge-compute box so the gateway Pi's
`tailscale-gateway-autonet` logic has a realistic, DHCP-less segment to infer
from. Each simulated host is a real Layer-2 neighbor (own MAC + static IP via a
Docker **macvlan** network), continuously emitting ARP, unicast pings, and
directed broadcasts. It doubles as targets for the dashboard's `arp-scan`.

## Topology

```
        ┌────────────┐  cellular WAN (internet)
        │ Peplink BR2│────────────────────────────
        │  test VLAN │  192.168.77.0/24, gateway .1, DHCP OFF
        └─────┬──────┘
              │ (wired)
      ┌───────┴───────────┐
      │                   │
┌─────┴──────┐     ┌──────┴───────────────┐
│ Pi under   │     │ edge box (this sim)  │
│ test       │     │ host-a .61           │
│ (autonet)  │     │ host-b .62           │
└────────────┘     │ host-c .63           │
                   │ [lldp] (optional)    │
                   └──────────────────────┘
```

## Prerequisites

1. **BR2 test VLAN**: a network (e.g. `192.168.77.0/24`, gateway `.1`) with the
   **DHCP server disabled**, internet upstream via cellular. Keep it on its own
   VLAN so experiments don't touch your production gateway.
2. **Wired connection.** macvlan requires the segment to allow multiple MACs on
   one port — fine on a wired switch/VLAN, but **Wi-Fi APs reject this**, so the
   edge box must be wired.
3. Docker + Docker Compose v2 on the edge box.

## Usage

```bash
cd Test-Harness/network-simulator
cp .env.example .env
# edit .env: set PARENT_IFACE to the wired NIC, and SUBNET/GATEWAY/BROADCAST
# to match the BR2 test VLAN.

docker compose up -d --build        # start the 3 simulated hosts
docker compose logs -f              # watch the chatter

# With LLDP advertising too (exercises autonet's LLDP path):
docker compose --profile lldp up -d --build
```

Then boot the Pi-under-test on the same segment and watch it infer the network:

```bash
# on the Pi (serial console recommended, since it has no DHCP/tailnet yet)
journalctl -u tailscale-gateway-autonet -f
```

## Scenarios

- **Happy path** — run the sim, boot the Pi, confirm autonet infers
  `192.168.77.0/24` + gateway `.1`, picks a free high address, and reaches the
  internet.
- **IP-conflict / ACD** — point one host at the address autonet would grab
  first. Change a service's `ipv4_address` to `192.168.77.254`, `up -d`, and
  confirm autonet's conflict detection skips it and picks the next free one.
- **Netmask inference** — set the BR2 VLAN (and `.env` `SUBNET`/`BROADCAST`,
  plus the hosts' IPs) to a `/23` and confirm autonet derives `/23` from the
  directed broadcast rather than defaulting to `/24`.
- **Silent segment** — stop the sim (`docker compose down`) and boot the Pi:
  autonet should fail *gracefully* (expected — nothing to infer from).

## Scaling up

Copy a `host-*` block in `docker-compose.yml`, give it a new `ipv4_address` and
`SELF_NAME`, and add its IP to the other hosts' `PEERS`. More hosts = denser,
more realistic ARP/broadcast traffic.

## Caveats

- **The edge box can't reach its own macvlan containers** (a kernel macvlan
  limitation). That's fine here — the consumer is the *separate* Pi-under-test.
- If you change `SUBNET`, you must also update the hardcoded `ipv4_address`
  values in `docker-compose.yml`.
- The optional `lldp` service uses host networking and advertises on *all* host
  interfaces, not just the test NIC.

## Teardown

```bash
docker compose --profile lldp down      # stops everything incl. lldp
```
