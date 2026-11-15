#!/usr/bin/env bash
# ==============================================================================
# Web51TNC — Appliance First-Boot Provisioning (runs ON the Raspberry Pi)
# ==============================================================================
# Invoked once by web51tnc-firstboot.service shortly after boot, as root.
#
# Turns a freshly flashed image into the dual-homed APRS appliance:
#   1. eth0  -> static LAN for the NET-51TNC (192.168.1.1/24)
#   2. dnsmasq DHCP server on that LAN only (TNC reservation at .120 kept free)
#   3. nftables MASQUERADE so the TNC reaches APRS-IS through the Wi-Fi uplink
#   4. Deploys Web51TNC from /boot/firmware/web51tnc-bundle and installs the
#      systemd service (needs internet; skipped cleanly if the uplink is down)
#   5. Writes /boot/firmware/STATUS.TXT — pull the stick and read it to see
#      exactly how far a headless boot got.
#
# Re-runnable:  sudo /usr/local/sbin/web51tnc-firstboot.sh --force
# ==============================================================================

set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DEBIAN_FRONTEND=noninteractive

STATUS=/boot/firmware/STATUS.TXT
MARKER=/var/lib/web51tnc/.firstboot-done
APP_USER="pi"
APP_DIR="/home/${APP_USER}/web51tnc"
BUNDLE=/boot/firmware/web51tnc-bundle
LAN_IF="eth0"
LAN_ADDR="192.168.1.1"
LAN_CIDR="192.168.1.1/24"
LAN_NET="192.168.1.0/24"
TNC_IP="192.168.1.120"
WEB_PORT="8000"

FORCE=false
[[ "${1:-}" == "--force" ]] && FORCE=true

# ------------------------------------------------------------------------------
# Status log — plain text on the FAT boot partition, readable from any laptop
# ------------------------------------------------------------------------------
mount -o remount,rw /boot/firmware 2>/dev/null || true
: > "$STATUS" 2>/dev/null || STATUS=/tmp/STATUS.TXT
log()  { printf '%s%s\n' "$(date -Is) " "$*" >>"$STATUS"; sync; }
step() { log "=== $*"; }

step "Web51TNC first-boot provisioning"
log "model:   $(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo unknown)"
log "kernel:  $(uname -r)"
log "hostname:$(hostname)"

if [[ -f "$MARKER" && "$FORCE" != true ]]; then
    log "already provisioned (marker $MARKER) — nothing to do"
    echo "Already provisioned. Use --force to re-run." >&2
    exit 0
fi

# ------------------------------------------------------------------------------
# 1. Wait for the Wi-Fi uplink (network-online.target is not always truthful)
# ------------------------------------------------------------------------------
step "Waiting for WLAN uplink"
rfkill unblock all 2>/dev/null || true
REGDOM="$(sed -nE 's/.*cfg80211\.ieee80211_regdom=([A-Z]{2}).*/\1/p' /proc/cmdline 2>/dev/null | head -n1)"
[[ -n "$REGDOM" ]] && iw reg set "$REGDOM" 2>/dev/null || true
nmcli radio wifi on 2>/dev/null || true
for netpath in /sys/class/net/wlan* /sys/class/net/wlp*; do
    [[ -e "$netpath" ]] || continue
    wif="${netpath##*/}"
    ip link set "$wif" up 2>/dev/null || true
    nmcli device set "$wif" managed yes 2>/dev/null || true
    nmcli device connect "$wif" >/dev/null 2>&1 || true
done

WLAN_IP=""
for i in $(seq 1 60); do
    WLAN_IP="$(ip -4 -o addr show 2>/dev/null | awk '$2 ~ /^(wlan|wlp)/ {split($4,a,"/"); print a[1]; exit}')"
    [[ -n "$WLAN_IP" ]] && break
    if (( i % 5 == 0 )); then
        rfkill unblock all 2>/dev/null || true
        nmcli radio wifi on 2>/dev/null || true
        nmcli device connect wlan0 >/dev/null 2>&1 || true
    fi
    sleep 3
done
SSID="$(iwgetid -r 2>/dev/null || nmcli -t -f active,ssid dev wifi 2>/dev/null | awk -F: '$1=="yes"{print $2; exit}')"
log "wlan0 ip: ${WLAN_IP:-NONE}  ssid: ${SSID:-NONE}"
if [[ -z "$WLAN_IP" ]]; then
    log "rfkill:   $(rfkill list 2>/dev/null | tr '\n' ' ')"
    log "nmcli:    $(nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status 2>/dev/null | tr '\n' '|')"
fi

ONLINE=false
for _ in $(seq 1 10); do
    if ping -c1 -W2 1.1.1.1 >/dev/null 2>&1 || getent hosts deb.debian.org >/dev/null 2>&1; then
        ONLINE=true; break
    fi
    sleep 3
done
log "internet: ${ONLINE}"

# ------------------------------------------------------------------------------
# 2. LAN interface: static address for the NET-51TNC
# ------------------------------------------------------------------------------
step "Configuring ${LAN_IF} as ${LAN_CIDR}"
nmcli connection delete web51tnc-lan >/dev/null 2>&1 || true
nmcli connection add \
    type ethernet con-name web51tnc-lan ifname "$LAN_IF" \
    ipv4.method manual ipv4.addresses "$LAN_CIDR" ipv4.never-default yes \
    ipv6.method ignore \
    connection.autoconnect yes connection.autoconnect-priority 200 >/dev/null 2>&1 \
    && log "NetworkManager profile web51tnc-lan created" \
    || log "ERROR: could not create LAN profile"
nmcli connection up web51tnc-lan >/dev/null 2>&1 || log "LAN link down (no cable yet) — autoconnect armed"

# ------------------------------------------------------------------------------
# 3. DHCP server on the LAN port only
# ------------------------------------------------------------------------------
step "Installing DHCP server (dnsmasq, ${LAN_IF} only)"
cat > /etc/web51tnc-dnsmasq.conf <<EOF
# Web51TNC LAN DHCP — deliberately bound to ${LAN_IF} only.
# port=0: no DNS service (avoids clashing with NetworkManager's resolver);
# clients are pointed at public resolvers instead.
interface=${LAN_IF}
bind-dynamic
port=0
dhcp-authoritative
dhcp-range=192.168.1.50,192.168.1.110,255.255.255.0,12h
dhcp-option=option:router,${LAN_ADDR}
dhcp-option=option:dns-server,1.1.1.1,9.9.9.9
# ${TNC_IP} is intentionally outside the pool: the NET-51TNC is static.
EOF

cat > /etc/systemd/system/web51tnc-dhcp.service <<'EOF'
[Unit]
Description=Web51TNC LAN DHCP server (eth0 only)
After=network.target web51tnc-lan-ready.service
Wants=network.target

[Service]
Type=simple
ExecStart=/usr/sbin/dnsmasq --keep-in-foreground --conf-file=/etc/web51tnc-dnsmasq.conf
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
rm -f /etc/systemd/system/web51tnc-lan-ready.service
systemctl daemon-reload
systemctl enable --now web51tnc-dhcp.service >/dev/null 2>&1 \
    && log "web51tnc-dhcp.service: $(systemctl is-active web51tnc-dhcp.service)" \
    || log "ERROR: web51tnc-dhcp.service failed"

# ------------------------------------------------------------------------------
# 4. NAT gateway: LAN -> Wi-Fi uplink (nftables; iptables is not installed)
# ------------------------------------------------------------------------------
step "Enabling IPv4 forwarding + NAT (${LAN_NET} -> wlan)"
cat > /etc/sysctl.d/90-web51tnc.conf <<'EOF'
net.ipv4.ip_forward = 1
vm.swappiness = 1
EOF
sysctl -q -p /etc/sysctl.d/90-web51tnc.conf || true

cat > /etc/web51tnc-nat.nft <<EOF
add table ip web51tnc_nat
flush table ip web51tnc_nat
add chain ip web51tnc_nat postrouting { type nat hook postrouting priority 100 ; }
add rule ip web51tnc_nat postrouting ip saddr ${LAN_NET} oifname != "${LAN_IF}" masquerade
add chain ip web51tnc_nat forward
add rule ip web51tnc_nat forward ip saddr ${LAN_NET} accept
add rule ip web51tnc_nat forward ip daddr ${LAN_NET} ct state established,related accept
EOF

cat > /etc/systemd/system/web51tnc-nat.service <<'EOF'
[Unit]
Description=Web51TNC NAT gateway (eth0 -> Wi-Fi uplink)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/web51tnc-nat.nft

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now web51tnc-nat.service >/dev/null 2>&1 \
    && log "web51tnc-nat.service: $(systemctl is-active web51tnc-nat.service)" \
    || log "ERROR: web51tnc-nat.service failed"

# mDNS so the admin UI is reachable as http://<hostname>.local:8000
systemctl enable --now avahi-daemon >/dev/null 2>&1 || true
timedatectl set-timezone Europe/Helsinki >/dev/null 2>&1 || true

# ------------------------------------------------------------------------------
# 5. Web51TNC application + Wi-Fi Fallback AP
# ------------------------------------------------------------------------------
step "Deploying Web51TNC from ${BUNDLE}"
APP_DEPLOYED=false
if [[ ! -d "$BUNDLE" ]]; then
    log "ERROR: bundle missing — app not deployed"
else
    mkdir -p "$APP_DIR"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete "$BUNDLE/" "$APP_DIR/"
    else
        cp -a "$BUNDLE/." "$APP_DIR/"
    fi
    mkdir -p "$APP_DIR/backups"
    chown -R "${APP_USER}:${APP_USER}" "$APP_DIR"
    log "app files staged to $APP_DIR"

    # Install Wi-Fi Fallback AP controller + boot watchdog
    if [[ -f "$APP_DIR/rpi/web51tnc-ap-ctl.sh" ]]; then
        install -m 0755 "$APP_DIR/rpi/web51tnc-ap-ctl.sh" /usr/local/sbin/web51tnc-ap-ctl
    fi
    if [[ -f "$APP_DIR/rpi/web51tnc-wifi-fallback.service" ]]; then
        install -m 0644 "$APP_DIR/rpi/web51tnc-wifi-fallback.service" /etc/systemd/system/web51tnc-wifi-fallback.service
        systemctl daemon-reload
        systemctl enable web51tnc-wifi-fallback.service >/dev/null 2>&1 || true
    fi

    if [[ "$ONLINE" == true ]]; then
        apt-get update -qq >/dev/null 2>&1 || log "WARN: apt-get update failed"
        apt-get install -y --no-install-recommends python3-pip rsync >/dev/null 2>&1 \
            || log "WARN: python3-pip/rsync install failed"

        if { sudo -u "$APP_USER" python3 -m pip install --quiet --break-system-packages \
                -r "$APP_DIR/requirements.txt"; } >>"$STATUS" 2>&1; then
            log "python dependencies installed (online)"
        else
            log "ERROR: pip install failed"
        fi
    elif [[ -d "$APP_DIR/rpi/wheels" ]] && command -v pip3 >/dev/null 2>&1; then
        if { sudo -u "$APP_USER" python3 -m pip install --quiet --break-system-packages \
                --no-index --find-links="$APP_DIR/rpi/wheels" \
                -r "$APP_DIR/requirements.txt"; } >>"$STATUS" 2>&1; then
            log "python dependencies installed (offline wheelhouse)"
        fi
    else
        log "WARN: offline and no pip/wheels — checking if python deps already present"
    fi

    if [[ -f "$APP_DIR/web51tnc.service" ]]; then
        sed -e "s|^User=.*|User=${APP_USER}|" \
            -e "s|^WorkingDirectory=.*|WorkingDirectory=${APP_DIR}|" \
            -e "s|^ExecStart=.*|ExecStart=/usr/bin/python3 ${APP_DIR}/server.py|" \
            "$APP_DIR/web51tnc.service" > /etc/systemd/system/web51tnc.service
        systemctl daemon-reload
        if systemctl enable --now web51tnc.service >/dev/null 2>&1; then
            log "web51tnc.service: $(systemctl is-active web51tnc.service)"
            if sudo -u "$APP_USER" python3 -c "import fastapi, uvicorn, pydantic" >/dev/null 2>&1; then
                APP_DEPLOYED=true
            fi
        else
            log "ERROR: web51tnc.service failed"
        fi
    fi

    if [[ -z "$WLAN_IP" && -x /usr/local/sbin/web51tnc-ap-ctl ]]; then
        log "No Wi-Fi uplink — starting Hotspot AP Web51TNC-Setup (10.42.0.1)"
        /usr/local/sbin/web51tnc-ap-ctl start >>"$STATUS" 2>&1 || true
    fi
fi

# ------------------------------------------------------------------------------
# 6. Final report
# ------------------------------------------------------------------------------
step "Result"
for unit in web51tnc-dhcp web51tnc-nat web51tnc avahi-daemon ssh; do
    command -v systemctl >/dev/null && \
        log "$(printf '%-18s %s' "$unit" "$(systemctl is-active "$unit" 2>/dev/null)")"
done
log "addresses: $(ip -4 -o addr show | awk '{print $2"="$4}' | tr '\n' ' ')"
log "routes:    $(ip -4 route | tr '\n' '|')"
log "DONE — web UI: http://$(hostname).local:${WEB_PORT}  TNC: ${TNC_IP}"

if [[ "$APP_DEPLOYED" == true ]]; then
    mkdir -p "$(dirname "$MARKER")" && touch "$MARKER"
else
    log "NOTE: marker $MARKER not written so firstboot will retry app deploy on next boot"
fi
sync
exit 0
