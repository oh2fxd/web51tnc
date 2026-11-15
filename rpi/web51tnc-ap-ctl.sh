#!/usr/bin/env bash
# ==============================================================================
# Web51TNC — Wi-Fi Fallback Access Point Controller
# ==============================================================================
# Usage:
#   web51tnc-ap-ctl start      -> Start Hotspot AP (Web51TNC-Setup / web51tnc @ 10.42.0.1)
#   web51tnc-ap-ctl stop       -> Stop Hotspot AP and reconnect to client Wi-Fi
#   web51tnc-ap-ctl boot-check -> Wait up to 45s for client Wi-Fi; if offline, start AP
#   web51tnc-ap-ctl status     -> Print JSON/text status
# ==============================================================================

set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WLAN_IF="wlan0"
AP_CON="Web51TNC-AP"
AP_SSID="${WEB51TNC_AP_SSID:-Web51TNC-Setup}"
AP_PSK="${WEB51TNC_AP_PSK:-web51tnc}"
AP_IP="10.42.0.1"
AP_CIDR="10.42.0.1/24"
AP_DNSMASQ_CONF="/etc/web51tnc-ap-dnsmasq.conf"
AP_DNSMASQ_PID="/run/web51tnc-ap-dnsmasq.pid"

log() {
    logger -t web51tnc-ap "$*" 2>/dev/null || true
    echo "[web51tnc-ap] $*"
}

ensure_ap_profile() {
    if ! nmcli -t -f NAME connection show 2>/dev/null | grep -Fxq "$AP_CON"; then
        log "Creating NetworkManager hotspot profile $AP_CON (SSID: $AP_SSID)"
        nmcli connection add \
            type wifi ifname "$WLAN_IF" con-name "$AP_CON" \
            autoconnect no ssid "$AP_SSID" \
            802-11-wireless.mode ap 802-11-wireless.band bg \
            ipv4.method manual ipv4.addresses "$AP_CIDR" \
            ipv6.method ignore \
            wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$AP_PSK" >/dev/null 2>&1 || true
    fi
}

start_ap_dhcp() {
    cat > "$AP_DNSMASQ_CONF" <<EOF
interface=${WLAN_IF}
bind-dynamic
port=0
dhcp-authoritative
dhcp-range=10.42.0.10,10.42.0.60,255.255.255.0,1h
dhcp-option=option:router,${AP_IP}
dhcp-option=option:dns-server,${AP_IP},1.1.1.1
EOF
    if [[ -f "$AP_DNSMASQ_PID" ]] && kill -0 "$(cat "$AP_DNSMASQ_PID" 2>/dev/null)" 2>/dev/null; then
        kill "$(cat "$AP_DNSMASQ_PID")" 2>/dev/null || true
        sleep 0.5
    fi
    /usr/sbin/dnsmasq --conf-file="$AP_DNSMASQ_CONF" --pid-file="$AP_DNSMASQ_PID" 2>/dev/null || true
}

stop_ap_dhcp() {
    if [[ -f "$AP_DNSMASQ_PID" ]]; then
        kill "$(cat "$AP_DNSMASQ_PID" 2>/dev/null)" 2>/dev/null || true
        rm -f "$AP_DNSMASQ_PID"
    fi
}

start_ap() {
    rfkill unblock all 2>/dev/null || true
    nmcli radio wifi on 2>/dev/null || true
    ensure_ap_profile
    log "Activating Wi-Fi Hotspot AP '$AP_SSID' on ${WLAN_IF} (${AP_IP})"
    if nmcli connection up "$AP_CON" >/dev/null 2>&1; then
        start_ap_dhcp
        log "Hotspot AP active: SSID='${AP_SSID}' PSK='${AP_PSK}' URL=http://${AP_IP}:8000"
        return 0
    else
        log "ERROR: Failed to activate $AP_CON"
        return 1
    fi
}

stop_ap() {
    log "Stopping Wi-Fi Hotspot AP '$AP_CON'"
    stop_ap_dhcp
    nmcli connection down "$AP_CON" >/dev/null 2>&1 || true
    sleep 1
    nmcli device connect "$WLAN_IF" >/dev/null 2>&1 || true
}

is_client_connected() {
    local active_con
    active_con="$(nmcli -t -f DEVICE,STATE,CONNECTION dev status 2>/dev/null | awk -F: -v d="$WLAN_IF" '$1==d && $2=="connected" {print $3}')"
    if [[ -n "$active_con" && "$active_con" != "$AP_CON" ]]; then
        return 0
    fi
    return 1
}

boot_check() {
    log "Checking for client Wi-Fi connection on ${WLAN_IF} (waiting up to 45s)..."
    rfkill unblock all 2>/dev/null || true
    nmcli radio wifi on 2>/dev/null || true

    for _ in $(seq 1 15); do
        if is_client_connected; then
            local con
            con="$(nmcli -t -f DEVICE,CONNECTION dev status 2>/dev/null | awk -F: -v d="$WLAN_IF" '$1==d {print $2}')"
            log "Client Wi-Fi connected (${con}) — Hotspot AP not needed."
            exit 0
        fi
        sleep 3
    done

    log "No client Wi-Fi connection after 45s — starting fallback Hotspot AP '${AP_SSID}'"
    start_ap
}

case "${1:-status}" in
    start)
        start_ap
        ;;
    stop)
        stop_ap
        ;;
    boot-check)
        boot_check
        ;;
    status)
        nmcli -t -f DEVICE,TYPE,STATE,CONNECTION dev status 2>/dev/null | grep "^${WLAN_IF}:" || true
        ;;
    *)
        echo "Usage: $0 {start|stop|boot-check|status}" >&2
        exit 1
        ;;
esac
