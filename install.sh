#!/usr/bin/env bash
# Web51TNC Automated Installer for Raspberry Pi & Linux
# Installs dependencies, sets up systemd daemon, and optionally adds a secondary Ethernet IP.

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
REAL_USER="${SUDO_USER:-$USER}"
PYTHON_BIN="$(which python3 || true)"

echo -e "${BOLD}${BLUE}====================================================${NC}"
echo -e "${BOLD}${BLUE}    Web51TNC Installer — Raspberry Pi & Linux       ${NC}"
echo -e "${BOLD}${BLUE}====================================================${NC}"
echo -e "Install Directory: ${GREEN}${INSTALL_DIR}${NC}"
echo -e "Target User:       ${GREEN}${REAL_USER}${NC}"
echo ""

# Handle --uninstall
if [[ "$1" == "--uninstall" ]]; then
    echo -e "${YELLOW}Uninstalling Web51TNC system service...${NC}"
    if [[ $EUID -ne 0 ]]; then
        echo -e "${RED}Error: Root privileges required for uninstallation.${NC}"
        echo -e "Please run: sudo ./install.sh --uninstall"
        exit 1
    fi
    systemctl stop web51tnc.service 2>/dev/null || true
    systemctl disable web51tnc.service 2>/dev/null || true
    rm -f /etc/systemd/system/web51tnc.service
    systemctl daemon-reload
    echo -e "${GREEN}[OK] Web51TNC service removed successfully.${NC}"
    exit 0
fi

# 1. Verify Python 3.10+
if [ -z "$PYTHON_BIN" ]; then
    echo -e "${RED}[ERR] python3 was not found on your system!${NC}"
    echo "Please install Python 3.10 or newer: sudo apt install python3 python3-pip"
    exit 1
fi

PY_VER="$($PYTHON_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR="$(echo "$PY_VER" | cut -d. -f1)"
PY_MINOR="$(echo "$PY_VER" | cut -d. -f2)"

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo -e "${RED}[ERR] Python 3.10+ is required (found Python $PY_VER).${NC}"
    exit 1
fi
echo -e "${GREEN}[OK] Found Python $PY_VER ($PYTHON_BIN)${NC}"

# 2. Install Python Dependencies
echo ""
echo -e "${BOLD}[1/3] Installing Python Dependencies...${NC}"
if [ -f "$INSTALL_DIR/requirements.txt" ]; then
    if [[ $EUID -eq 0 && -n "$SUDO_USER" ]]; then
        if ! sudo -u "$REAL_USER" "$PYTHON_BIN" -m pip install --break-system-packages -r "$INSTALL_DIR/requirements.txt"; then
            sudo -u "$REAL_USER" "$PYTHON_BIN" -m pip install -r "$INSTALL_DIR/requirements.txt"
        fi
    else
        if ! "$PYTHON_BIN" -m pip install --break-system-packages --user -r "$INSTALL_DIR/requirements.txt"; then
            "$PYTHON_BIN" -m pip install --user -r "$INSTALL_DIR/requirements.txt"
        fi
    fi
    echo -e "${GREEN}[OK] Python packages installed successfully.${NC}"
else
    echo -e "${RED}[ERR] requirements.txt not found in $INSTALL_DIR!${NC}"
    exit 1
fi

# 3. Optional Systemd Service Setup (rendered from web51tnc.service template)
INSTALL_SERVICE=false
PROMPT_SERVICE=true
for arg in "$@"; do
    if [[ "$arg" == "--service" || "$arg" == "-s" || "$arg" == "-y" || "$arg" == "--yes" ]]; then
        INSTALL_SERVICE=true
        PROMPT_SERVICE=false
    elif [[ "$arg" == "--no-service" ]]; then
        INSTALL_SERVICE=false
        PROMPT_SERVICE=false
    fi
done

if [ "$PROMPT_SERVICE" = true ] && [ -t 0 ]; then
    echo ""
    read -r -p "Do you want to install and enable the Web51TNC background systemd service? [y/N]: " choice
    case "$choice" in
        [yY][eE][sS]|[yY])
            INSTALL_SERVICE=true
            ;;
        *)
            INSTALL_SERVICE=false
            ;;
    esac
fi

if [ "$INSTALL_SERVICE" = true ]; then
    echo ""
    echo -e "${BOLD}[2/3] Configuring systemd Service...${NC}"
    SERVICE_FILE="/etc/systemd/system/web51tnc.service"
    TEMPLATE_FILE="$INSTALL_DIR/web51tnc.service"

    if [ -f "$TEMPLATE_FILE" ]; then
        SERVICE_CONTENT="$(sed \
            -e "s|^User=.*|User=${REAL_USER}|" \
            -e "s|^WorkingDirectory=.*|WorkingDirectory=${INSTALL_DIR}|" \
            -e "s|^ExecStart=.*|ExecStart=${PYTHON_BIN} ${INSTALL_DIR}/server.py|" \
            "$TEMPLATE_FILE")"
    else
        echo -e "${RED}[ERR] Template $TEMPLATE_FILE not found!${NC}"
        exit 1
    fi

    if [[ $EUID -eq 0 ]]; then
        echo "$SERVICE_CONTENT" > "$SERVICE_FILE"
        systemctl daemon-reload
        systemctl enable web51tnc.service
        systemctl restart web51tnc.service
    else
        echo -e "${YELLOW}Notice: Root privileges are required to install systemd services.${NC}"
        TMP_SVC="$(mktemp)"
        echo "$SERVICE_CONTENT" > "$TMP_SVC"
        sudo mv "$TMP_SVC" "$SERVICE_FILE"
        sudo systemctl daemon-reload
        sudo systemctl enable web51tnc.service
        sudo systemctl restart web51tnc.service
    fi
    echo -e "${GREEN}[OK] Web51TNC service installed, enabled, and started.${NC}"
fi

# 4. Optional Secondary Ethernet IP Alias Setup (Non-destructive: adds +ipv4.addresses without replacing DHCP/Gateway)
SETUP_ETH=false
if [ -t 0 ]; then
    echo ""
    read -r -p "Add secondary IP alias (192.168.1.50/24) to wired Ethernet for direct NET-51TNC access? [y/N]: " eth_choice
    case "$eth_choice" in
        [yY][eE][sS]|[yY])
            SETUP_ETH=true
            ;;
        *)
            SETUP_ETH=false
            ;;
    esac
fi

if [ "$SETUP_ETH" = true ]; then
    echo ""
    echo -e "${BOLD}[3/3] Adding Secondary Ethernet IP Alias (192.168.1.50/24)...${NC}"
    ETH_IF="$(ip -o link show | awk -F': ' '{print $2}' | grep -E '^(eth0|end0|enp)' | head -n 1 || true)"
    if [ -n "$ETH_IF" ]; then
        echo -e "Detected Ethernet interface: ${GREEN}$ETH_IF${NC}"
        if command -v nmcli >/dev/null 2>&1; then
            CON_NAME="$(nmcli -t -f NAME,DEVICE connection show --active | grep ":$ETH_IF$" | cut -d: -f1 || true)"
            if [ -n "$CON_NAME" ]; then
                echo "Appending secondary address 192.168.1.50/24 via NetworkManager on '$CON_NAME'..."
                sudo nmcli connection modify "$CON_NAME" +ipv4.addresses 192.168.1.50/24
                sudo nmcli connection up "$CON_NAME"
            else
                sudo ip addr add 192.168.1.50/24 dev "$ETH_IF"
            fi
        else
            sudo ip addr add 192.168.1.50/24 dev "$ETH_IF"
            sudo ip link set "$ETH_IF" up
        fi
        echo -e "${GREEN}[OK] Secondary alias 192.168.1.50/24 added on $ETH_IF.${NC}"
    else
        echo -e "${YELLOW}[WARN] No standard wired Ethernet interface (eth0/end0/enp*) detected.${NC}"
    fi
fi

echo ""
echo -e "${BOLD}${GREEN}====================================================${NC}"
echo -e "${BOLD}${GREEN}           Web51TNC Installation Complete!          ${NC}"
echo -e "${BOLD}${GREEN}====================================================${NC}"
echo -e "Web Interface:   ${BOLD}http://localhost:8000${NC} (or your Pi IP: http://<pi-ip>:8000)"
echo -e "Launch manually: ${YELLOW}./run.sh${NC}"
echo -e "Check daemon:    ${YELLOW}sudo systemctl status web51tnc${NC}"
echo -e "View live logs:  ${YELLOW}sudo journalctl -u web51tnc -f${NC}"
echo ""
