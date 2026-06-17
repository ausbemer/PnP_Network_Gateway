#!/usr/bin/env bash
# tailscale-gateway-autonet.sh
# ---------------------------------------------------------------------------
# Auto-configuration fallback for networks WITHOUT DHCP.
#
# Boot flow:
#   1. Wait up to DHCP_WAIT seconds for a normal DHCP lease.
#   2. If a lease appears  -> do nothing (DHCP path is preferred).
#   3. If no lease         -> passively sniff the segment, infer the subnet and
#                             gateway, pick a free address with RFC 5227 Address
#                             Conflict Detection, configure it tentatively,
#                             verify internet reachability, and only then commit.
#
# This runs BEFORE tailscale-gateway.service, so by the time the gateway starts
# there is a real default route for it to detect — the rest of the stack is
# unchanged.
#
# Heuristic by nature: a totally silent segment yields little to infer from, and
# the netmask is the least certain value (defaults to /24 unless a directed
# broadcast or LLDP says otherwise). All active steps are conflict-checked.
#
# Can also be re-run at any time (it is idempotent: if a default route already
# exists it exits immediately), which is how the watcher supports hot-swapping
# into a no-DHCP network.
# ---------------------------------------------------------------------------

set -uo pipefail

DHCP_WAIT="${DHCP_WAIT:-15}"      # seconds to wait for DHCP before falling back
SNIFF_SECS="${SNIFF_SECS:-20}"    # passive capture window
ACD_TRIES="${ACD_TRIES:-20}"      # max candidate addresses to conflict-check
GATEWAY_TRIES="${GATEWAY_TRIES:-5}"  # max gateway candidates to test before defaulting to .1
PING_TARGETS=(1.1.1.1 9.9.9.9 8.8.8.8)

# --dry-run: sniff, infer, and report what WOULD be configured, changing nothing.
DRY_RUN=0
for arg in "$@"; do
    case "${arg}" in
        -n|--dry-run) DRY_RUN=1 ;;
        -h|--help)
            cat <<EOF
Usage: $(basename "$0") [--dry-run]

  Configures the network on a DHCP-less segment by sniffing and inferring it.

  -n, --dry-run   Sniff and report the inferred subnet/gateway and the address
                  that would be claimed, WITHOUT applying any configuration.
                  (Still sends ARP conflict-detection probes; no addresses or
                  routes are set, and internet reachability is not tested.)
  -h, --help      Show this help.
EOF
            exit 0 ;;
        *) echo "Unknown argument: ${arg}" >&2; exit 2 ;;
    esac
done

LOG() {
    if [[ ${DRY_RUN} -eq 1 ]]; then echo "autonet[dry-run]: $*"; else echo "autonet: $*"; fi
}

# ── Persist a copy of this run's output to the boot (FAT) partition ────────────
# Lets you pull the SD card and read autonet.log on any computer after a failed
# run, and is mounted into the dashboard for reading over the tailnet on success.
# Output still flows to stdout (journald) as well.
setup_logging() {
    local d
    AUTONET_LOG=""
    for d in /boot/firmware /boot /var/log; do
        if [[ -d "${d}" && -w "${d}" ]]; then AUTONET_LOG="${d}/autonet.log"; break; fi
    done
    [[ -z "${AUTONET_LOG}" ]] && return 0   # nowhere writable; stick to stdout

    # Bound the file size. The dashboard mounts the directory (not the file),
    # so replacing the inode here is safe.
    if [[ -f "${AUTONET_LOG}" ]] \
       && (( $(wc -l < "${AUTONET_LOG}" 2>/dev/null || echo 0) > 2000 )); then
        tail -n 1000 "${AUTONET_LOG}" > "${AUTONET_LOG}.tmp" 2>/dev/null \
            && mv "${AUTONET_LOG}.tmp" "${AUTONET_LOG}"
    fi

    echo "===== autonet run $(date -Is 2>/dev/null) (dry_run=${DRY_RUN}) =====" \
        >> "${AUTONET_LOG}" 2>/dev/null || true
    # Mirror everything from here on to the log file, keeping stdout intact.
    exec > >(tee -a "${AUTONET_LOG}") 2>&1
}

# ── Integer <-> dotted-quad helpers ───────────────────────────────────────────
ip2int() { local a b c d; IFS=. read -r a b c d <<<"$1"; echo $(((a<<24)+(b<<16)+(c<<8)+d)); }
int2ip() { local i=$1; echo "$(((i>>24)&255)).$(((i>>16)&255)).$(((i>>8)&255)).$((i&255))"; }
mask_int() { local p=$1; echo $(( p==0 ? 0 : (0xFFFFFFFF << (32-p)) & 0xFFFFFFFF )); }

# ── Interface selection ───────────────────────────────────────────────────────
# Pick the wired interface that has a carrier and isn't one of ours.
primary_iface() {
    local dev
    for dev in /sys/class/net/*; do
        local name; name=$(basename "${dev}")
        case "${name}" in lo|tailscale*|docker*|veth*|br-*) continue ;; esac
        [[ "$(cat "${dev}/operstate" 2>/dev/null)" == "up" ]] || continue
        [[ "$(cat "${dev}/carrier" 2>/dev/null)" == "1" ]] || continue
        printf '%s' "${name}"; return 0
    done
    return 1
}

# ── DHCP detection ────────────────────────────────────────────────────────────
# We consider DHCP "done" when there is a global-scope IPv4 address AND a
# default route (link-local 169.254/16 from IPv4LL does not count).
have_l3() {
    ip -4 route show default 2>/dev/null | grep -q . \
        && ip -4 -o addr show scope global 2>/dev/null | grep -q .
}

wait_for_dhcp() {
    local i
    for ((i=0; i<DHCP_WAIT; i++)); do
        have_l3 && return 0
        sleep 1
    done
    return 1
}

# ── Passive capture ───────────────────────────────────────────────────────────
# Capture ARP + common broadcast/multicast chatter for SNIFF_SECS. Output is raw
# tcpdump text on stdout for the parsers below.
sniff() {
    local iface=$1
    ip link set dev "${iface}" up 2>/dev/null || true
    # -Q in is not portable across all builds; capture both directions, no names.
    timeout "${SNIFF_SECS}" tcpdump -nni "${iface}" -e -l \
        'arp or (ip and ip broadcast) or (udp port 5353) or (udp port 5355) or (udp port 1900) or (udp port 137)' \
        2>/dev/null
}

# ── Inference (PURE functions: read capture text on stdin) ────────────────────
# Gateway guess: the IPv4 address that the most distinct hosts ARP for.
infer_gateway() {
    grep -oE 'who-has [0-9.]+ tell [0-9.]+' \
        | awk '{print $2}' \
        | sort | uniq -c | sort -rn \
        | awk 'NR==1{print $2}'
}

# Ranked list of ARP'd-for IPs (most-requested first), one per line. The real
# gateway is usually near the top, but not always — so we test several.
infer_gateways() {
    grep -oE 'who-has [0-9.]+ tell [0-9.]+' \
        | awk '{print $2}' \
        | sort | uniq -c | sort -rn \
        | awk '{print $2}'
}

# Hosts known to be on-segment: every "tell X" sender and every "is-at" owner.
# Capture stdin once: two greps reading the same pipe would race (the first
# drains it before the second sees anything).
infer_hosts() {
    local cap; cap=$(cat)
    {
        printf '%s\n' "${cap}" | grep -oE 'tell [0-9.]+'        | awk '{print $2}'
        printf '%s\n' "${cap}" | grep -oE 'Reply [0-9.]+ is-at' | awk '{print $2}'
    } | sort -u
}

# Directed broadcast destination, if any IPv4 broadcast frame was seen.
# tcpdump -e prints "... > ff:ff:ff:ff:ff:ff ... A.B.C.D.<port> > W.X.Y.Z.<port>"
infer_broadcast() {
    # A frame to the all-ones MAC whose dst IP looks like a subnet broadcast
    # (commonly x.x.x.255) reveals the prefix. Take the most frequent one.
    grep -i 'ff:ff:ff:ff:ff:ff' \
        | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.255' \
        | sort | uniq -c | sort -rn | awk 'NR==1{print $2}'
}

# Given an anchor host IP and an observed broadcast address, find the prefix
# whose subnet broadcast equals that address. Falls back to 24.
infer_prefix() {
    local anchor=$1 bcast=${2:-}
    if [[ -z "${bcast}" ]]; then echo 24; return; fi
    local ai bi p m net bc
    ai=$(ip2int "${anchor}"); bi=$(ip2int "${bcast}")
    for ((p=30; p>=16; p--)); do
        m=$(mask_int "${p}")
        net=$(( ai & m ))
        bc=$(( net | (0xFFFFFFFF & ~m) ))
        if (( bc == bi )); then echo "${p}"; return; fi
    done
    echo 24
}

# ── Subnet discovery (for multi-homing on a shared segment) ───────────────────
# Cluster every on-segment IP into its /24 and list the networks present, busiest
# first. An unmanaged switch joining two subnets shows up here as two entries.
detect_networks() {
    local cap; cap=$(cat)
    {
        printf '%s\n' "${cap}" | grep -oE 'tell [0-9.]+'        | awk '{print $2}'
        printf '%s\n' "${cap}" | grep -oE 'who-has [0-9.]+'     | awk '{print $2}'
        printf '%s\n' "${cap}" | grep -oE 'Reply [0-9.]+ is-at' | awk '{print $2}'
    } | awk -F. 'NF==4 {print $1"."$2"."$3".0/24"}' \
      | sort | uniq -c | sort -rn | awk '{print $2}'
}

# ── Address Conflict Detection (RFC 5227) ─────────────────────────────────────
# Returns 0 if the address is FREE (no ARP reply), 1 if in use.
ip_is_free() {
    local iface=$1 cand=$2
    # arping -D: duplicate-address-detection probe sourced from 0.0.0.0.
    if arping -D -q -c 2 -w 2 -I "${iface}" "${cand}" >/dev/null 2>&1; then
        return 0   # exit 0 => no reply => free
    fi
    return 1
}

# Choose a free host address in the subnet, scanning high-to-low (static
# assignments cluster low), skipping network/broadcast/gateway/known hosts.
pick_free_ip() {
    local iface=$1 net=$2 prefix=$3 gw=$4 used=$5
    local m neti bci first last cand tries=0
    m=$(mask_int "${prefix}")
    neti=$(( $(ip2int "${net}") & m ))
    bci=$(( neti | (0xFFFFFFFF & ~m) ))
    first=$(( neti + 1 )); last=$(( bci - 1 ))
    local gwi; gwi=$(ip2int "${gw}")
    for ((cand=last; cand>=first; cand--)); do
        (( tries++ > ACD_TRIES )) && break
        (( cand == gwi )) && continue
        local dq; dq=$(int2ip "${cand}")
        case " ${used} " in *" ${dq} "*) continue ;; esac
        if ip_is_free "${iface}" "${dq}"; then printf '%s' "${dq}"; return 0; fi
    done
    return 1
}

# ── Connectivity test (TCP, not ICMP) ─────────────────────────────────────────
# Many firewalls — industrial routers like the Siemens Scalance especially —
# silently drop outbound ICMP while passing real traffic. So we verify with a
# TCP connect, not ping, to avoid false "no internet" verdicts.
test_internet() {
    local hp h p
    for hp in 1.1.1.1:443 8.8.8.8:443 9.9.9.9:443; do
        h=${hp%:*}; p=${hp#*:}
        if timeout 3 bash -c "exec 3<>/dev/tcp/${h}/${p}" 2>/dev/null; then
            return 0
        fi
    done
    return 1
}

# Set the default route via a candidate gateway and verify real internet through
# it. The address is assumed already assigned. Leaves the route in place on
# success; removes it on failure so the next candidate starts clean.
verify_via() {
    local iface=$1 gw=$2
    ip route replace default via "${gw}" dev "${iface}" 2>/dev/null || return 1
    if test_internet; then return 0; fi
    ip route del default via "${gw}" dev "${iface}" 2>/dev/null || true
    return 1
}

# ── Keep DHCP as the default behavior (self-heal stale static pins) ────────────
# A roaming device must try DHCP on every network it's plugged into. Older builds
# pinned the NM connection to a manual address (to stop NM stripping our config),
# but that PERMANENTLY broke DHCP on the next network. Here we undo such a pin:
# if the connection is set to manual, reset it to auto so a lease is requested.
# Only acts when the method is 'manual', so a healthy DHCP connection is untouched.
ensure_dhcp_default() {
    command -v nmcli >/dev/null 2>&1 || return 0
    local iface=$1 con method
    con=$(nmcli -t -g GENERAL.CONNECTION device show "${iface}" 2>/dev/null)
    [[ -z "${con}" ]] && return 0
    method=$(nmcli -g ipv4.method connection show "${con}" 2>/dev/null)
    if [[ "${method}" == "manual" ]]; then
        LOG "NM connection '${con}' was pinned to manual — resetting to DHCP"
        nmcli con mod "${con}" ipv4.method auto \
            ipv4.addresses "" ipv4.gateway "" ipv4.dns "" 2>/dev/null || true
        nmcli con up "${con}" 2>/dev/null || true
        sleep 2
    fi
}

# ── Ensure a working resolver (session-scoped only) ───────────────────────────
# We deliberately do NOT pin a static NetworkManager profile — that's what broke
# DHCP portability. Addresses and routes are applied via `ip` for this session
# and simply re-applied on each boot by this service.
commit() {
    local iface=$1 gw=$2
    if ! grep -q '^nameserver' /etc/resolv.conf 2>/dev/null; then
        { [[ -n "${gw}" ]] && echo "nameserver ${gw}"; echo "nameserver 1.1.1.1"; } \
            >> /etc/resolv.conf 2>/dev/null || true
    fi
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
    setup_logging

    # Self-heal: undo any stale manual NM pin from older builds so DHCP is tried
    # on this network before we consider auto-static.
    local hwif; hwif=$(primary_iface) && ensure_dhcp_default "${hwif}"

    # Establish which interface and whether we already have L3 (DHCP).
    local had_l3=0 iface
    if have_l3; then
        had_l3=1
    else
        LOG "waiting up to ${DHCP_WAIT}s for DHCP..."
        wait_for_dhcp && { had_l3=1; LOG "DHCP succeeded"; }
    fi
    if [[ ${had_l3} -eq 1 ]]; then
        iface=$(ip -4 route show default 2>/dev/null | awk '/default/{print $5; exit}')
    else
        iface=$(primary_iface) || { LOG "no live interface found"; exit 1; }
        LOG "no DHCP on ${iface}; entering auto-static mode"
    fi
    [[ -z "${iface}" ]] && { LOG "could not determine interface"; exit 1; }

    # Sniff the segment to discover the subnet(s) present. An unmanaged switch
    # joining two networks shows up as two subnets here.
    local cap; cap=$(sniff "${iface}")
    if [[ -z "${cap}" ]]; then
        [[ ${had_l3} -eq 1 ]] && { LOG "online via DHCP; no extra segment traffic seen"; exit 0; }
        LOG "captured no traffic; cannot infer network"; exit 1
    fi

    local hosts ranked nets have_nets
    hosts=$(printf '%s\n' "${cap}" | infer_hosts | tr '\n' ' ')
    ranked=$(printf '%s\n' "${cap}" | infer_gateways)         # ARP'd IPs, busiest first
    nets=$(printf '%s\n' "${cap}" | detect_networks)          # /24s, busiest first
    [[ -z "${nets}" ]] && { LOG "no subnets observed; giving up"
        [[ ${had_l3} -eq 1 ]] && exit 0 || exit 1; }
    LOG "subnets on segment: $(printf '%s ' ${nets})"

    # Subnets we already hold an address in (e.g. from DHCP) — don't re-claim.
    have_nets=$(ip -4 route show dev "${iface}" proto kernel 2>/dev/null | awk '{print $1}')

    # Claim a free address in each subnet, and gather gateway candidates. We
    # multi-home across every subnet so all are locally reachable (and can be
    # advertised over Tailscale); only ONE becomes the default route.
    local gw_candidates=() seen_gw=" " primary_net="" netcidr net m neti bci gw1 gwlast topgw g myip
    for netcidr in ${nets}; do
        net=${netcidr%/*}
        [[ -z "${primary_net}" ]] && primary_net="${net}"
        m=$(mask_int 24); neti=$(( $(ip2int "${net}") & m )); bci=$(( neti | 0xFF ))
        gw1=$(int2ip $(( neti + 1 ))); gwlast=$(int2ip $(( bci - 1 )))
        topgw=$(printf '%s\n' ${ranked} | awk -v p="${net%.*}." 'index($0,p)==1{print; exit}')

        if printf '%s\n' ${have_nets} | grep -qx "${netcidr}"; then
            LOG "already addressed in ${netcidr}"
        elif myip=$(pick_free_ip "${iface}" "${net}" 24 "${gw1}" "${hosts} ${gw1} ${gwlast} ${topgw}"); then
            if [[ ${DRY_RUN} -eq 1 ]]; then
                LOG "WOULD claim ${myip}/24 in ${netcidr}"
            elif ip addr add "${myip}/24" dev "${iface}" 2>/dev/null; then
                LOG "claimed ${myip}/24 in ${netcidr}"
            else
                LOG "could not add ${myip}/24 in ${netcidr}"
            fi
        else
            LOG "no free address found in ${netcidr}"
        fi

        for g in "${topgw}" "${gw1}" "${gwlast}"; do
            [[ -z "${g}" ]] && continue
            case "${seen_gw}" in *" ${g} "*) continue ;; esac
            gw_candidates+=("${g}"); seen_gw+="${g} "
        done
    done
    ip link set "${iface}" up 2>/dev/null || true

    # Default route: keep DHCP's if present; else pick the subnet that actually
    # reaches the internet.
    if [[ ${had_l3} -eq 1 ]]; then
        [[ ${DRY_RUN} -eq 1 ]] && { LOG "dry run complete (DHCP provides the default route)"; exit 0; }
        LOG "default route already present (DHCP) — left as-is"
        LOG "auto-network complete; all connected subnets will be advertised via Tailscale"
        exit 0
    fi

    # Cap candidates so a noisy multi-subnet segment can't spin.
    while (( ${#gw_candidates[@]} > GATEWAY_TRIES )); do
        unset 'gw_candidates[$(( ${#gw_candidates[@]} - 1 ))]'
    done

    if [[ ${DRY_RUN} -eq 1 ]]; then
        LOG "WOULD test default gateways in order: ${gw_candidates[*]}"
        LOG "dry run complete — no addresses, routes, or DNS changed"
        exit 0
    fi

    local chosen=""
    for g in "${gw_candidates[@]}"; do
        LOG "trying default via ${g}..."
        if verify_via "${iface}" "${g}"; then chosen="${g}"; break; fi
        LOG "  no internet via ${g}"
    done
    if [[ -z "${chosen}" ]]; then
        chosen=$(int2ip $(( ($(ip2int "${primary_net}") & $(mask_int 24)) + 1 )))
        LOG "no candidate reached the internet — defaulting to ${chosen} (unverified)"
        ip route replace default via "${chosen}" dev "${iface}" 2>/dev/null || true
    fi
    commit "${iface}" "${chosen}"
    LOG "auto-network complete: default via ${chosen}; all connected subnets advertised via Tailscale"
    exit 0
}

main "$@"
