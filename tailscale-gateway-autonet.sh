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

# ── Tentative configure + verify, with rollback ───────────────────────────────
try_config() {
    local iface=$1 ip=$2 prefix=$3 gw=$4 t ok=1
    LOG "trying ${ip}/${prefix} via ${gw} on ${iface}"
    ip addr add "${ip}/${prefix}" dev "${iface}" 2>/dev/null || return 1
    ip route add default via "${gw}" dev "${iface}" 2>/dev/null || true
    # Gateway must answer at L2/L3, then verify real internet.
    if ping -c1 -W1 "${gw}" >/dev/null 2>&1; then
        for t in "${PING_TARGETS[@]}"; do
            if ping -c1 -W2 "${t}" >/dev/null 2>&1; then ok=0; break; fi
        done
    fi
    if (( ok == 0 )); then
        LOG "verified internet via ${gw}"
        return 0
    fi
    LOG "no connectivity with ${ip}/${prefix} via ${gw} — rolling back"
    ip route del default via "${gw}" dev "${iface}" 2>/dev/null || true
    ip addr del "${ip}/${prefix}" dev "${iface}" 2>/dev/null || true
    return 1
}

# ── Persist via NetworkManager so it isn't stripped; fall back to raw ip ───────
commit() {
    local iface=$1 ip=$2 prefix=$3 gw=$4
    if command -v nmcli >/dev/null 2>&1; then
        local con
        con=$(nmcli -t -g GENERAL.CONNECTION device show "${iface}" 2>/dev/null)
        if [[ -n "${con}" ]]; then
            LOG "persisting static config on NM connection '${con}'"
            nmcli con mod "${con}" \
                ipv4.method manual \
                ipv4.addresses "${ip}/${prefix}" \
                ipv4.gateway "${gw}" \
                ipv4.dns "${gw} 1.1.1.1" 2>/dev/null || true
            nmcli con up "${con}" 2>/dev/null || true
            return 0
        fi
    fi
    LOG "NetworkManager not managing ${iface}; leaving raw ip config in place"
    grep -q '^nameserver' /etc/resolv.conf 2>/dev/null \
        || echo "nameserver 1.1.1.1" >> /etc/resolv.conf
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
    setup_logging
    if have_l3; then LOG "default route already present — nothing to do"; exit 0; fi

    LOG "waiting up to ${DHCP_WAIT}s for DHCP..."
    if wait_for_dhcp; then LOG "DHCP succeeded"; exit 0; fi

    local iface; iface=$(primary_iface) || { LOG "no live interface found"; exit 1; }
    LOG "no DHCP on ${iface}; entering auto-static mode"

    local cap; cap=$(sniff "${iface}")
    [[ -z "${cap}" ]] && { LOG "captured no traffic; cannot infer network"; exit 1; }

    local gw hosts bcast prefix anchor
    gw=$(printf '%s\n' "${cap}" | infer_gateway)
    hosts=$(printf '%s\n' "${cap}" | infer_hosts | tr '\n' ' ')
    bcast=$(printf '%s\n' "${cap}" | infer_broadcast)

    # Fall back to .1 then .254 of the densest /24 if no gateway stood out.
    anchor="${gw:-}"
    [[ -z "${anchor}" ]] && anchor=$(printf '%s\n' ${hosts} | head -1)
    [[ -z "${anchor}" ]] && { LOG "no host addresses observed; giving up"; exit 1; }

    prefix=$(infer_prefix "${anchor}" "${bcast}")

    if [[ -z "${gw}" ]]; then
        # Guess gateway as .1 of the inferred subnet.
        local m neti; m=$(mask_int "${prefix}"); neti=$(( $(ip2int "${anchor}") & m ))
        gw=$(int2ip $(( neti + 1 )))
        LOG "no gateway in capture; guessing ${gw}"
    fi

    local net; { local m neti; m=$(mask_int "${prefix}"); neti=$(( $(ip2int "${anchor}") & m )); net=$(int2ip "${neti}"); }
    LOG "inferred subnet ${net}/${prefix}, gateway ${gw}"

    local cand
    cand=$(pick_free_ip "${iface}" "${net}" "${prefix}" "${gw}" "${hosts}") \
        || { LOG "could not find a free address"; exit 1; }

    if [[ ${DRY_RUN} -eq 1 ]]; then
        LOG "WOULD configure ${cand}/${prefix} via ${gw} on ${iface}"
        LOG "observed on-segment hosts: ${hosts:-none}"
        LOG "dry run complete — no addresses, routes, or DNS changed"
        exit 0
    fi

    if try_config "${iface}" "${cand}" "${prefix}" "${gw}"; then
        commit "${iface}" "${cand}" "${prefix}" "${gw}"
        LOG "auto-static configuration complete: ${cand}/${prefix} via ${gw}"
        exit 0
    fi

    LOG "auto-static configuration failed"
    exit 1
}

main "$@"
