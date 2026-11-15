#!/usr/bin/env bash
# ==============================================================================
# Web51TNC — Offline Raspberry Pi OS Image Provisioner (USB / SD)
# ==============================================================================
# Bakes Wi-Fi + user + hostname provisioning straight into a Raspberry Pi OS
# image *before* it is flashed. No root privileges are needed for provisioning
# (it uses debugfs/mtools on a plain .img file), so the same image can be
# flashed to a microSD card, a USB stick or an NVMe drive afterwards.
#
# Provisions:
#   - Bookworm/Trixie `userconf.txt`  -> user + password, SSH enabled
#   - NetworkManager keyfile         -> Wi-Fi SSID/PSK (autoconnect, prio 100)
#   - cmdline.txt                    -> cfg80211.ieee80211_regdom=<CC>
#   - /etc/hostname + /etc/hosts     -> appliance hostname
#   - Wi-Fi powersave disabled       -> stable long-running link
#
# Usage:
#   ./rpi/provision-usb-image.sh --image rpi/build/pi-lite.img
#   ./rpi/provision-usb-image.sh --image pi.img --ssid MyWiFi --psk secret \
#                                --hostname web51tnc --country FI --user pi
#   sudo ./rpi/provision-usb-image.sh --image pi.img --flash /dev/sdX
#
# Notes:
#   - `--flash` is the ONLY step that needs root (raw block device write).
#   - Bookworm/Trixie only: `wpa_supplicant.conf` on the boot partition is
#     ignored by modern images, hence the NetworkManager keyfile below.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()  { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
step()  { echo -e "${BOLD}${BLUE}==>${NC} $*"; }
die()   { echo -e "${RED}[ERR]${NC} $*" >&2; exit 1; }

IMAGE=""
FLASH_DEV=""
SSID="${SSID:-}"
SECOND_SSID="${SECOND_SSID:-}"
PSK="${PSK:-}"
PI_USER="${PI_USER:-pi}"
PI_PASS="${PI_PASS:-raspberry}"
PI_HOSTNAME="${PI_HOSTNAME:-web51tnc}"
WIFI_COUNTRY="${WIFI_COUNTRY:-FI}"
HIDDEN="${HIDDEN:-false}"
KEEP_RESIZE="${KEEP_RESIZE:-false}"
TARGET_SIZE_GIB="${TARGET_SIZE_GIB:-4}"
PASS_HASH_OVERRIDE="${PASS_HASH_OVERRIDE:-}"
NO_VERIFY=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --image)    IMAGE="$2"; shift 2 ;;
        --flash)    FLASH_DEV="$2"; shift 2 ;;
        --ssid)     SSID="$2"; shift 2 ;;
        --ssid-alt) SECOND_SSID="$2"; shift 2 ;;
        --psk)      PSK="$2"; shift 2 ;;
        --user)     PI_USER="$2"; shift 2 ;;
        --password) PI_PASS="$2"; shift 2 ;;
        --pass-hash) PASS_HASH_OVERRIDE="$2"; shift 2 ;;
        --hostname) PI_HOSTNAME="$2"; shift 2 ;;
        --country)  WIFI_COUNTRY="$2"; shift 2 ;;
        --hidden)   HIDDEN=true; shift ;;
        --keep-resize) KEEP_RESIZE=true; shift ;;
        --no-verify) NO_VERIFY=true; shift ;;
        -h|--help)  sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
done

[[ -n "$IMAGE" ]] || die "No --image given (run with --help)"
[[ -f "$IMAGE" ]] || die "Image not found: $IMAGE"

# ------------------------------------------------------------------------------
# 0. Sanity checks: tooling and (optionally) compressed image
# ------------------------------------------------------------------------------
step "Checking host tooling"
for t in debugfs e2fsck resize2fs mcopy mdir sfdisk openssl truncate; do
    command -v "$t" >/dev/null 2>&1 || die "Missing required tool: $t (install e2fsprogs + mtools)"
done
info "debugfs, e2fsck, resize2fs, mtools, sfdisk, openssl present"

if [[ "$IMAGE" == *.xz ]]; then
    step "Decompressing ${IMAGE}"
    xz -d -k -T0 "$IMAGE"
    IMAGE="${IMAGE%.xz}"
    info "Extracted: $IMAGE"
fi

[[ "$PI_PASS" == "raspberry" ]] && \
    warn "Using default password 'raspberry' — change it after first login."

# ------------------------------------------------------------------------------
# 1. Discover partition layout & pre-expand rootfs offline (avoids USB brownout)
# ------------------------------------------------------------------------------
step "Reading partition table"
LAYOUT="$(sfdisk -d "$IMAGE" 2>/dev/null)" || die "Cannot parse partition table"
BOOT_START="$(awk '/start=/ && $0 ~ /type=(c|b|e)/ {match($0,/start=[ ]*[0-9]+/); print substr($0,RSTART+6,RLENGTH-6); exit}' <<<"$LAYOUT")"
ROOT_START="$(awk '/start=/ && $0 ~ /type=83/ {match($0,/start=[ ]*[0-9]+/); print substr($0,RSTART+6,RLENGTH-6); exit}' <<<"$LAYOUT")"

[[ -n "$BOOT_START" && -n "$ROOT_START" ]] || die "Could not locate boot (FAT) / root (ext4) partitions"
BOOT_OFF=$(( BOOT_START * 512 ))
ROOT_OFF=$(( ROOT_START * 512 ))
info "bootfs @ byte ${BOOT_OFF}  |  rootfs @ byte ${ROOT_OFF}"

IMGF="$(realpath "$IMAGE")"
FAT_SPEC="${IMGF}@@${BOOT_OFF}"
EXT_SPEC="${IMGF}?offset=${ROOT_OFF}"

if [[ "$KEEP_RESIZE" != true ]]; then
    TARGET_BYTES=$(( TARGET_SIZE_GIB * 1024 * 1024 * 1024 ))
    CUR_BYTES="$(stat -c %s "$IMGF")"
    if (( CUR_BYTES < TARGET_BYTES )); then
        step "Pre-expanding image offline to ${TARGET_SIZE_GIB} GiB (prevents first-boot 248GB USB resize crash)"
        truncate -s "${TARGET_BYTES}" "$IMGF"
        echo ", +" | sfdisk --no-reread -N 2 "$IMGF" >/dev/null 2>&1 || die "sfdisk partition 2 expand failed"
        ROOT_SECTORS="$(sfdisk -d "$IMGF" 2>/dev/null | awk '/start=/ && $0 ~ /type=83/ {match($0,/size=[ ]*[0-9]+/); print substr($0,RSTART+5,RLENGTH-5); exit}')"
        ROOT_BLOCKS_4K=$(( (ROOT_SECTORS * 512) / 4096 ))
        e2fsck -fy "$EXT_SPEC" >/dev/null 2>&1 || true
        resize2fs "$EXT_SPEC" "${ROOT_BLOCKS_4K}" >/dev/null 2>&1 || die "resize2fs failed"
        e2fsck -fy "$EXT_SPEC" >/dev/null 2>&1 || true
        info "rootfs expanded offline to ${ROOT_BLOCKS_4K} 4K-blocks (${TARGET_SIZE_GIB} GiB image)"
    fi
fi

WORK="$(mktemp -d -t web51tnc-provision.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

# debugfs wrapper: operate on the rootfs inside the image
extfs() { debugfs -w -R "$*" "$EXT_SPEC" 2>/dev/null; }

# Write a file into the ext4 rootfs (replace if present) with given mode/owner.
# NOTE: ext4 inode mode includes the file type bits (S_IFREG = 0100000), so a
# plain "0644" would create a "bad type" inode -> always OR in 0100000.
ext_put() { # <local-file> <target-path> <octal-mode>
    local src="$1" dst="$2" mode="$3"
    extfs "rm $dst" >/dev/null 2>&1 || true
    debugfs -w -R "write $src $dst" "$EXT_SPEC" >/dev/null 2>&1 \
        || die "debugfs write failed for $dst"
    extfs "sif $dst mode 0100${mode}"
    extfs "sif $dst uid 0"
    extfs "sif $dst gid 0"
    # Fail fast if the inode did not end up as a regular file
    extfs "stat $dst" | grep -qi "Type: regular" \
        || die "debugfs produced a non-regular inode for $dst"
}

# ------------------------------------------------------------------------------
# 2. Boot partition: userconf.txt + ssh flag + Trixie cloud-init (NoCloud)
# ------------------------------------------------------------------------------
step "Provisioning bootfs (user + SSH + Trixie cloud-init)"
if [[ -n "$PASS_HASH_OVERRIDE" ]]; then
    PASS_HASH="$PASS_HASH_OVERRIDE"
else
    PASS_HASH="$(openssl passwd -6 -salt "$(openssl rand -hex 8)" "$PI_PASS")"
fi
printf '%s:%s\n' "$PI_USER" "$PASS_HASH" > "$WORK/userconf.txt"
mcopy -o -i "$FAT_SPEC" "$WORK/userconf.txt" ::userconf.txt \
    || die "Failed to write userconf.txt"
: > "$WORK/ssh"
mcopy -o -i "$FAT_SPEC" "$WORK/ssh" ::ssh || die "Failed to write ssh flag"

# Raspberry Pi OS Trixie (2025/2026+) uses cloud-init NoCloud (/boot/firmware/user-data
# & meta-data) which masks userconfig.service and locks the 'pi' password if left
# at stock defaults. Populate both so Bookworm and Trixie both work cleanly.
cat > "$WORK/meta-data" <<EOF
dsmode: local
instance_id: web51tnc-appliance
local-hostname: ${PI_HOSTNAME}
EOF
mcopy -o -i "$FAT_SPEC" "$WORK/meta-data" ::meta-data \
    || die "Failed to write meta-data"

cat > "$WORK/user-data" <<EOF
#cloud-config
hostname: ${PI_HOSTNAME}
manage_etc_hosts: true
preserve_hostname: false
ssh_pwauth: true
enable_ssh: true
users:
  - name: ${PI_USER}
    gecos: Web51TNC Operator
    groups: [adm, dialout, cdrom, audio, users, sudo, video, games, plugdev, input, gpio, spi, i2c, netdev, render, lpadmin]
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
    shell: /bin/bash
    lock_passwd: false
    passwd: "${PASS_HASH}"
runcmd:
  - [ rfkill, unblock, all ]
  - [ iw, reg, set, "${WIFI_COUNTRY}" ]
  - [ nmcli, radio, wifi, on ]
EOF
mcopy -o -i "$FAT_SPEC" "$WORK/user-data" ::user-data \
    || die "Failed to write user-data"
info "userconf.txt + ssh + cloud-init (user-data/meta-data) for '${PI_USER}' written"

# ------------------------------------------------------------------------------
# 3. cmdline.txt: regulatory domain + USB storage quirks + remove firstboot resize
# ------------------------------------------------------------------------------
step "Configuring cmdline.txt (regdom=${WIFI_COUNTRY}, USB quirks, resize=${KEEP_RESIZE})"
mtype -i "$FAT_SPEC" ::cmdline.txt > "$WORK/cmdline.txt" || die "cmdline.txt missing"
CMDLINE_CLEAN="$(tr -d '\r\n' < "$WORK/cmdline.txt" \
    | sed -E "s/[[:space:]]*cfg80211\.ieee80211_regdom=[^[:space:]]*//g" \
    | sed -E "s/[[:space:]]*usb-storage\.quirks=[^[:space:]]*//g" \
    | sed -E "s/[[:space:]]*usbcore\.autosuspend=[^[:space:]]*//g" \
    | tr -s ' ' \
    | sed -E 's/^ +//; s/ +$//')"
if [[ "$KEEP_RESIZE" != true ]]; then
    CMDLINE_CLEAN="$(sed -E 's/(^|[[:space:]])resize($|[[:space:]])/ /g' <<<"$CMDLINE_CLEAN" | tr -s ' ' | sed -E 's/^ +//; s/ +$//')"
fi
printf '%s cfg80211.ieee80211_regdom=%s usb-storage.quirks=0951:1666:u usbcore.autosuspend=-1\n' \
    "$CMDLINE_CLEAN" "$WIFI_COUNTRY" > "$WORK/cmdline.new"
mcopy -o -i "$FAT_SPEC" "$WORK/cmdline.new" ::cmdline.txt \
    || die "Failed to update cmdline.txt"
info "cmdline.txt: $(mtype -i "$FAT_SPEC" ::cmdline.txt)"

# ------------------------------------------------------------------------------
# 4. Rootfs: hostname, direct user/SSH unlock, Wi-Fi keyfile, powersave, rfkill
# ------------------------------------------------------------------------------
step "Provisioning rootfs"

printf '%s\n' "$PI_HOSTNAME" > "$WORK/hostname"
ext_put "$WORK/hostname" /etc/hostname 644

extfs "dump /etc/hosts $WORK/hosts" >/dev/null 2>&1 || die "Cannot read /etc/hosts"
sed -i "s/\braspberrypi\b/${PI_HOSTNAME}/g" "$WORK/hosts"
grep -q "127.0.1.1[[:space:]]*${PI_HOSTNAME}" "$WORK/hosts" || \
    printf '127.0.1.1\t%s\n' "$PI_HOSTNAME" >> "$WORK/hosts"
ext_put "$WORK/hosts" /etc/hosts 644

# Directly unlock the user in /etc/passwd + /etc/shadow and enable ssh.service
# so login works even before cloud-init / userconfig.service runs.
extfs "dump /etc/passwd $WORK/passwd" >/dev/null 2>&1 || die "Cannot read /etc/passwd"
sed -i -E "s|^(${PI_USER}:[^:]*:[^:]*:[^:]*:[^:]*:[^:]*):/usr/sbin/nologin|\1:/bin/bash|" "$WORK/passwd"
ext_put "$WORK/passwd" /etc/passwd 644

extfs "dump /etc/shadow $WORK/shadow" >/dev/null 2>&1 || die "Cannot read /etc/shadow"
awk -F: -v u="$PI_USER" -v h="$PASS_HASH" 'BEGIN{OFS=":"} $1==u {$2=h} {print}' "$WORK/shadow" > "$WORK/shadow.new"
ext_put "$WORK/shadow.new" /etc/shadow 640
extfs "sif /etc/shadow gid 42" >/dev/null 2>&1 || true

extfs "rm /etc/ssh/sshd_config.d/rename_user.conf" >/dev/null 2>&1 || true
extfs "rm /etc/systemd/system/multi-user.target.wants/ssh.service" >/dev/null 2>&1 || true
debugfs -w -R "symlink /etc/systemd/system/multi-user.target.wants/ssh.service /lib/systemd/system/ssh.service" \
    "$EXT_SPEC" >/dev/null 2>&1 || true

# Two profiles so the Pi prefers the band the admin laptop is on (5 GHz), but
# still associates if only 2.4 GHz is in range — avoids "same SSID name, other
# band, isolated AP" surprises when debugging headless.
write_wifi_profile() { # <ssid> <autoconnect-priority>
    local ssid="$1" prio="$2" uuid
    uuid="$(cat /proc/sys/kernel/random/uuid)"
    cat > "$WORK/wifi.nmconnection" <<EOF
[connection]
id=${ssid}
uuid=${uuid}
type=wifi
autoconnect=true
autoconnect-priority=${prio}
autoconnect-retries=0
permissions=

[wifi]
mode=infrastructure
ssid=${ssid}
hidden=${HIDDEN}
powersave=2

[wifi-security]
key-mgmt=wpa-psk
psk=${PSK}

[ipv4]
method=auto

[ipv6]
method=auto
addr-gen-mode=stable-privacy
EOF
    chmod 600 "$WORK/wifi.nmconnection"
    ext_put "$WORK/wifi.nmconnection" \
        "/etc/NetworkManager/system-connections/${ssid}.nmconnection" 600
    info "Wi-Fi profile '${ssid}' (priority ${prio}, hidden=${HIDDEN}, powersave off)"
}

if [[ -n "$SSID" ]]; then
    write_wifi_profile "$SSID" 100
    if [[ -n "$SECOND_SSID" && "$SECOND_SSID" != "$SSID" ]]; then
        write_wifi_profile "$SECOND_SSID" 200
    fi
else
    info "No pre-configured Wi-Fi SSID given — image will start Hotspot AP 'Web51TNC-Setup' (10.42.0.1:8000) on first boot"
fi

cat > "$WORK/wifi-nopowersave.conf" <<'EOF'
# Web51TNC: disable Wi-Fi power saving (stable long-running link)
[connection]
wifi.powersave = 2
EOF
ext_put "$WORK/wifi-nopowersave.conf" /etc/NetworkManager/conf.d/99-web51tnc-wifi.conf 644

# NetworkManager must not remember a stale "Wi-Fi disabled" state
if extfs "stat /var/lib/NetworkManager/NetworkManager.state" | grep -q Inode; then
    extfs "dump /var/lib/NetworkManager/NetworkManager.state $WORK/nm.state" >/dev/null 2>&1 || true
    if [[ -f "$WORK/nm.state" ]]; then
        sed -i 's/^WirelessEnabled=.*/WirelessEnabled=true/' "$WORK/nm.state"
        ext_put "$WORK/nm.state" /var/lib/NetworkManager/NetworkManager.state 644
        info "NetworkManager WirelessEnabled=true"
    fi
fi

# wlan rfkill state must be "unblocked" (0) across all RPi SoC SDIO/MMC paths
printf '0\n' > "$WORK/rfkill0"
for rf_name in \
    "platform-3f300000.mmcnr:wlan" \
    "platform-fe300000.mmcnr:wlan" \
    "platform-1000fff000.mmc:wlan" \
    "platform-1f00110000.mmc:wlan" \
    "platform-soc:wlan" \
    $(extfs "ls /var/lib/systemd/rfkill" | awk '/:wlan/ {print $NF}'); do
    [[ -n "$rf_name" ]] || continue
    ext_put "$WORK/rfkill0" "/var/lib/systemd/rfkill/${rf_name}" 644
done

# ------------------------------------------------------------------------------
# 5. Appliance first-boot hook: LAN DHCP + NAT + app deploy run on the Pi
# ------------------------------------------------------------------------------
step "Installing appliance first-boot provisioning + Wi-Fi Fallback AP"
ext_put "$SCRIPT_DIR/appliance-firstboot.sh" \
    /usr/local/sbin/web51tnc-firstboot.sh 755
ext_put "$SCRIPT_DIR/web51tnc-firstboot.service" \
    /etc/systemd/system/web51tnc-firstboot.service 644
ext_put "$SCRIPT_DIR/web51tnc-ap-ctl.sh" \
    /usr/local/sbin/web51tnc-ap-ctl 755
ext_put "$SCRIPT_DIR/web51tnc-wifi-fallback.service" \
    /etc/systemd/system/web51tnc-wifi-fallback.service 644

# Enable them exactly the way `systemctl enable` would
extfs "rm /etc/systemd/system/multi-user.target.wants/web51tnc-firstboot.service" \
    >/dev/null 2>&1 || true
debugfs -w -R "symlink /etc/systemd/system/multi-user.target.wants/web51tnc-firstboot.service /etc/systemd/system/web51tnc-firstboot.service" \
    "$EXT_SPEC" >/dev/null 2>&1 || die "Failed to enable web51tnc-firstboot.service"
extfs "rm /etc/systemd/system/multi-user.target.wants/web51tnc-wifi-fallback.service" \
    >/dev/null 2>&1 || true
debugfs -w -R "symlink /etc/systemd/system/multi-user.target.wants/web51tnc-wifi-fallback.service /etc/systemd/system/web51tnc-wifi-fallback.service" \
    "$EXT_SPEC" >/dev/null 2>&1 || die "Failed to enable web51tnc-wifi-fallback.service"
info "web51tnc-firstboot.service + web51tnc-wifi-fallback.service enabled"

# ------------------------------------------------------------------------------
# 6. Stage the app itself on the FAT boot partition for offline deployment
# ------------------------------------------------------------------------------
step "Staging Web51TNC bundle on bootfs (/boot/firmware/web51tnc-bundle)"
rm -rf "$WORK/web51tnc-bundle"
mkdir -p "$WORK/web51tnc-bundle"
rsync -a \
    --exclude=".git" \
    --exclude="rpi/build" \
    --exclude="__pycache__" \
    --exclude=".pytest_cache" \
    --exclude=".ruff_cache" \
    --exclude="docs/*.pdf" \
    "$REPO_ROOT/" "$WORK/web51tnc-bundle/"
mcopy -o -s -i "$FAT_SPEC" "$WORK/web51tnc-bundle" ::/ \
    || die "Failed to copy bundle to bootfs"
info "bundle staged ($(du -sh "$WORK/web51tnc-bundle" | cut -f1))"

# Ensure ext4 filesystem metadata and journal are 100% consistent after debugfs writes
e2fsck -fy "$EXT_SPEC" >/dev/null 2>&1 || true

# ------------------------------------------------------------------------------
# 7. Verify the bake by reading it back out of the image
# ------------------------------------------------------------------------------
if [[ "$NO_VERIFY" != true ]]; then
    step "Verifying provisioned image"
    mtype -i "$FAT_SPEC" ::userconf.txt | grep -q "^${PI_USER}:" \
        || die "Verification failed: userconf.txt"
    mtype -i "$FAT_SPEC" ::user-data | grep -q "name: ${PI_USER}" \
        || die "Verification failed: cloud-init user-data"
    mtype -i "$FAT_SPEC" ::meta-data | grep -q "local-hostname: ${PI_HOSTNAME}" \
        || die "Verification failed: cloud-init meta-data"
    mtype -i "$FAT_SPEC" ::cmdline.txt | grep -q "ieee80211_regdom=${WIFI_COUNTRY}" \
        || die "Verification failed: cmdline.txt regdom"
    mdir -i "$FAT_SPEC" ::ssh >/dev/null 2>&1 || die "Verification failed: ssh flag"
    mdir -i "$FAT_SPEC" ::/web51tnc-bundle >/dev/null 2>&1 \
        || die "Verification failed: app bundle on bootfs"
    extfs "cat /etc/hostname" | grep -qx "$PI_HOSTNAME" \
        || die "Verification failed: /etc/hostname"
    extfs "cat /etc/passwd" | grep -q "^${PI_USER}:.*:/bin/bash$" \
        || die "Verification failed: /etc/passwd shell unlock"
    for s in "$SSID" "$SECOND_SSID"; do
        [[ -n "$s" ]] || continue
        extfs "stat /etc/NetworkManager/system-connections/${s}.nmconnection" \
            | grep -qi "mode: *0600" || die "Verification failed: NM keyfile for $s"
        extfs "cat /etc/NetworkManager/system-connections/${s}.nmconnection" \
            | grep -qx "autoconnect=true" || die "Verification failed: autoconnect for $s"
    done
    extfs "stat /usr/local/sbin/web51tnc-firstboot.sh" \
        | grep -qi "Type: regular" || die "Verification failed: firstboot script"
    extfs "stat /etc/systemd/system/multi-user.target.wants/web51tnc-firstboot.service" \
        | grep -qi "Type: symlink" || die "Verification failed: firstboot enable symlink"
    e2fsck -n "$EXT_SPEC" >/dev/null 2>&1 \
        || die "Verification failed: ext4 filesystem check"
    info "All checks passed"
fi

# ------------------------------------------------------------------------------
# 8. Optional: flash to block device (needs root)
# ------------------------------------------------------------------------------
if [[ -n "$FLASH_DEV" ]]; then
    [[ "$(id -u)" -eq 0 ]] || die "--flash requires root: re-run with sudo"
    [[ -b "$FLASH_DEV" ]] || die "Not a block device: $FLASH_DEV"
    step "Unmounting any active partitions on $FLASH_DEV"
    for mp_dev in $(lsblk -ln -o NAME "$FLASH_DEV" | tail -n +2); do
        umount "/dev/${mp_dev}" 2>/dev/null || umount -l "/dev/${mp_dev}" 2>/dev/null || true
    done
    if lsblk -no MOUNTPOINTS "$FLASH_DEV" | grep -q .; then
        die "$FLASH_DEV still has mounted partitions — unmount them first"
    fi
    DEV_BYTES="$(blockdev --getsize64 "$FLASH_DEV")"
    IMG_BYTES="$(stat -c %s "$IMAGE")"
    [[ "$DEV_BYTES" -ge "$IMG_BYTES" ]] || \
        die "Image ($IMG_BYTES B) is larger than $FLASH_DEV ($DEV_BYTES B)"
    step "Flashing $IMAGE -> $FLASH_DEV"
    dd if="$IMAGE" of="$FLASH_DEV" bs=4M oflag=direct conv=fsync status=progress
    sync
    blockdev --flushbufs "$FLASH_DEV" 2>/dev/null || true
    partprobe "$FLASH_DEV" 2>/dev/null || blockdev --rereadpt "$FLASH_DEV" 2>/dev/null || true
    sleep 1
    info "Flash complete"
fi

echo ""
echo -e "${BOLD}${GREEN}Image ready:${NC} ${IMAGE}"
echo -e "  user:     ${BOLD}${PI_USER}${NC}"
echo -e "  hostname: ${BOLD}${PI_HOSTNAME}.local${NC}"
echo -e "  Wi-Fi:    ${BOLD}${SSID}${NC} (prio 100) + ${BOLD}${SECOND_SSID:-none}${NC} (prio 200), country ${WIFI_COUNTRY}"
echo -e "  LAN:      eth0 static 192.168.1.1/24 + DHCP pool .50-.110 + NAT (first boot)"
echo -e "  status:   /boot/firmware/STATUS.TXT is written on first boot"
if [[ -z "$FLASH_DEV" ]]; then
    echo ""
    echo -e "${BOLD}Flash it (unmount USB stick first, then root required):${NC}"
    echo -e "  udisksctl unmount -b /dev/sdX1; udisksctl unmount -b /dev/sdX2"
    echo -e "  sudo ${BASH_SOURCE[0]} --image ${IMAGE} --flash /dev/sdX"
fi
