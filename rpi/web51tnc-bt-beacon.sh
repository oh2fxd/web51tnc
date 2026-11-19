#!/usr/bin/env bash
# ==============================================================================
# Web51TNC — Non-Connectable Bluetooth IP:Port Beacon
# ==============================================================================
# Advertises the Raspberry Pi's current IP:Port (e.g. "51TNC 192.168.32.137:8000")
# over both Classic BT discovery and non-connectable BLE advertising so an
# end-user can read the dashboard URL from any phone/PC Bluetooth scan without
# being able to connect or pair over Bluetooth.
# ==============================================================================

set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WEB_PORT="${WEB51TNC_PORT:-8000}"
STATE_FILE="/run/web51tnc-bt-beacon.last"

get_primary_ip() {
    local ip=""
    ip="$(ip -4 -o addr show scope global 2>/dev/null \
        | awk '$2 ~ /^(wlan|wlp)/ {split($4,a,"/"); print a[1]; exit}')"
    if [[ -z "$ip" ]]; then
        ip="$(ip -4 -o addr show scope global 2>/dev/null \
            | awk '$2 !~ /^eth0$/ {split($4,a,"/"); print a[1]; exit}')"
    fi
    if [[ -z "$ip" ]]; then
        ip="$(ip -4 -o addr show scope global 2>/dev/null \
            | awk '{split($4,a,"/"); print a[1]; exit}')"
    fi
    printf '%s' "$ip"
}

IP_ADDR="$(get_primary_ip)"
if [[ -n "$IP_ADDR" ]]; then
    LABEL="51TNC ${IP_ADDR}:${WEB_PORT}"
else
    LABEL="51TNC NO-IP:${WEB_PORT}"
fi

# Max 26 chars so 020106 (3B) + len+0x09 (2B) + name (<=26B) <= 31B BLE ADV limit
LABEL="${LABEL:0:26}"

command -v btmgmt >/dev/null 2>&1 || exit 0
rfkill unblock bluetooth 2>/dev/null || true

if ! compgen -G "/sys/class/bluetooth/hci*" >/dev/null; then
    logger -t web51tnc-bt "No Bluetooth controller found in /sys/class/bluetooth" 2>/dev/null || true
    exit 0
fi

# Ensure bluetoothd adapter properties (permanent discoverable, non-pairable)
if command -v busctl >/dev/null 2>&1; then
    timeout 3 busctl set-property org.bluez /org/bluez/hci0 org.bluez.Adapter1 Powered b true </dev/null >/dev/null 2>&1 || true
    timeout 3 busctl set-property org.bluez /org/bluez/hci0 org.bluez.Adapter1 Pairable b false </dev/null >/dev/null 2>&1 || true
    timeout 3 busctl set-property org.bluez /org/bluez/hci0 org.bluez.Adapter1 DiscoverableTimeout u 0 </dev/null >/dev/null 2>&1 || true
    timeout 3 busctl set-property org.bluez /org/bluez/hci0 org.bluez.Adapter1 Alias s "$LABEL" </dev/null >/dev/null 2>&1 || true
    timeout 3 busctl set-property org.bluez /org/bluez/hci0 org.bluez.Adapter1 Discoverable b true </dev/null >/dev/null 2>&1 || true
fi

if [[ -f "$STATE_FILE" ]] && [[ "$(cat "$STATE_FILE" 2>/dev/null)" == "$LABEL" ]]; then
    timeout 3 btmgmt discov on </dev/null >/dev/null 2>&1 || true
    exit 0
fi

# Ensure controller is powered, non-connectable, non-pairable, discoverable
timeout 3 btmgmt power on </dev/null >/dev/null 2>&1 || true
timeout 3 btmgmt le on </dev/null >/dev/null 2>&1 || true
timeout 3 btmgmt connectable off </dev/null >/dev/null 2>&1 || true
timeout 3 btmgmt pairable off </dev/null >/dev/null 2>&1 || true
timeout 3 btmgmt bondable off </dev/null >/dev/null 2>&1 || true
timeout 3 btmgmt name "$LABEL" </dev/null >/dev/null 2>&1 || true
timeout 3 btmgmt discov on </dev/null >/dev/null 2>&1 || true

# Build non-connectable BLE advertising payload:
#   Flags: 02 01 06 (LE General Discoverable, BR/EDR Not Supported)
#   Complete Local Name (0x09): <len+1> 09 <ascii-hex>
NAME_HEX="$(printf '%s' "$LABEL" | od -An -tx1 -v | tr -d ' \n')"
NAME_LEN=$(( ${#LABEL} + 1 ))
LEN_HEX="$(printf '%02x' "$NAME_LEN")"
ADV_HEX="020106${LEN_HEX}09${NAME_HEX}"

timeout 3 btmgmt rm-adv 1 </dev/null >/dev/null 2>&1 || true
# Note: omit -c so BLE advertisement is strictly non-connectable
timeout 3 btmgmt add-adv -g -d "$ADV_HEX" 1 </dev/null >/dev/null 2>&1 || true

printf '%s' "$LABEL" > "$STATE_FILE" 2>/dev/null || true
logger -t web51tnc-bt "Advertising non-connectable BT beacon: ${LABEL}" 2>/dev/null || true
