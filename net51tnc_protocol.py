"""
NET-51TNC / APRS 51TNC Protocol & Packet Parser
Reverse-engineered from APRS 51TNC SETUP _ENGLISH_20140701.exe (BH4TDV)
"""

import logging
import re
from enum import Enum
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, List

logger = logging.getLogger("net51tnc")

# Control commands (sent as raw ASCII over TCP port 60000)
CMD_READ = b"#HELLO"
CMD_RESET = b"#RESET"
CMD_INSTALL = b"#INSTALL"
CMD_GATE_RF_TEST = b"#GATE_RF_TEST"
CMD_GATE_IS_TEST = b"#GATE_IS_TEST"
CMD_WX_RF_TEST = b"#WX_RF_TEST"
CMD_WX_IS_TEST = b"#WX_IS_TEST"

DEFAULT_PORT = 60000
DEFAULT_IP = "192.168.1.120"

# Path mapping for ComboBox2, ComboBox3, ComboBox5
PATH_OPTIONS = ["- - -", "WIDE1-1", "WIDE1-1,WIDE2-1"]
BAUD_OPTIONS = ["Clever 4800", "Text 9600", "KISS 9600", "Clever 9600"]
PTT_DELAY_OPTIONS = ["250ms", "500ms", "750ms", "1000ms", "1250ms", "1500ms"]
MIN_INTERVAL_OPTIONS = ["250ms", "500ms", "750ms", "1000ms"]
TX_WAITING_OPTIONS = ["250ms", "500ms", "750ms", "1000ms"]
LCD_TYPE_OPTIONS = ["Close", "LCD12864(ST7920)", "Color 320x240"]
GPS_MODE_OPTIONS = ["Close", "Timing", "SmartBeacon", "Manual_IO2"]


# Explicit State Machine Enums (UX / DX / AX Alignment)
class ConnectionState(str, Enum):
    """Lifecycle states of the connection between Web51TNC and the NET-51TNC hardware."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    REBOOTING = "rebooting"
    ERROR = "error"


class ConfigSyncState(str, Enum):
    """Synchronization states of device EEPROM configuration."""

    UNKNOWN = "unknown"
    SYNCING = "syncing"
    SYNCED = "synced"
    DIRTY = "dirty"
    SAVING = "saving"
    ERROR = "error"


class StationFreshness(str, Enum):
    """Freshness lifecycle states for heard APRS stations."""

    ACTIVE = "active"  # Heard < 15 minutes ago
    RECENT = "recent"  # Heard 15..60 minutes ago
    STALE = "stale"  # Heard > 60 minutes ago


def compute_station_freshness(
    last_heard_iso: str, now: Optional[datetime] = None
) -> Tuple[StationFreshness, float]:
    """
    Computes station freshness lifecycle state and age in seconds.
    """
    if not last_heard_iso:
        return StationFreshness.STALE, float("inf")
    try:
        dt = datetime.fromisoformat(last_heard_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        cur = now or datetime.now(timezone.utc)
        age_sec = max(0.0, (cur - dt).total_seconds())
        if age_sec < 900.0:  # 15 minutes
            return StationFreshness.ACTIVE, age_sec
        elif age_sec < 3600.0:  # 60 minutes
            return StationFreshness.RECENT, age_sec
        else:
            return StationFreshness.STALE, age_sec
    except (ValueError, TypeError):
        return StationFreshness.STALE, float("inf")


class StreamFramerState(str, Enum):
    STREAM = "stream"
    READING_CONFIG = "reading_config"


class StreamFramer:
    """
    State machine for TCP byte stream framing.
    Correctly buffers and recovers fragmented lines across TCP read boundaries,
    and cleanly isolates binary #HELLO (518 bytes) configuration transfers
    even if preceded by in-flight packet bytes or fragmented across chunks.
    """

    def __init__(self, max_buffer_size: int = 16384):
        self.state = StreamFramerState.STREAM
        self._line_buffer = ""
        self._raw_buffer = bytearray()
        self._config_buffer = bytearray()
        self._max_buffer_size = max_buffer_size

    def set_state(self, state: StreamFramerState):
        self.state = state
        if state == StreamFramerState.READING_CONFIG:
            self._config_buffer.clear()

    def feed(self, chunk: bytes) -> Dict[str, Any]:
        """
        Feeds a TCP chunk into the framer state machine.
        Returns a dict:
          - "lines": list of completed text lines (if any)
          - "config_ready": bytes if #HELLO 518-byte payload is completed, else None
          - "trailing_text": str of remaining text if any accompanied the config frame
        """
        result: Dict[str, Any] = {
            "lines": [],
            "config_ready": None,
            "trailing_text": "",
        }

        if not chunk:
            return result

        if self.state == StreamFramerState.READING_CONFIG:
            self._config_buffer.extend(chunk)
            idx = self._config_buffer.find(b"#HELLO")
            if idx == -1:
                # Keep at most last 5 bytes in case #HELLO is split across chunk boundary
                if len(self._config_buffer) > 5:
                    pre = bytes(self._config_buffer[:-5])
                    del self._config_buffer[:-5]
                    result["lines"].extend(self._process_text_chunk(pre))
                return result

            if idx > 0:
                pre = bytes(self._config_buffer[:idx])
                del self._config_buffer[:idx]
                result["lines"].extend(self._process_text_chunk(pre))

            if len(self._config_buffer) >= 518:
                payload = bytes(self._config_buffer[:518])
                remaining = bytes(self._config_buffer[518:])
                self._config_buffer.clear()
                self.state = StreamFramerState.STREAM
                result["config_ready"] = payload
                if remaining:
                    result["lines"].extend(self._process_text_chunk(remaining))
            return result

        # STREAM mode: also detect unsolicited #HELLO frames (even across chunks)
        self._raw_buffer.extend(chunk)
        idx = self._raw_buffer.find(b"#HELLO")
        if idx != -1:
            if idx > 0:
                pre = bytes(self._raw_buffer[:idx])
                del self._raw_buffer[:idx]
                result["lines"].extend(self._process_text_chunk(pre))
            if len(self._raw_buffer) >= 518:
                result["config_ready"] = bytes(self._raw_buffer[:518])
                trailing = bytes(self._raw_buffer[518:])
                self._raw_buffer.clear()
                if trailing:
                    result["lines"].extend(self._process_text_chunk(trailing))
            else:
                # Transition to READING_CONFIG until remaining bytes arrive
                self._config_buffer.extend(self._raw_buffer)
                self._raw_buffer.clear()
                self.state = StreamFramerState.READING_CONFIG
            return result

        # Check if buffer ends with a partial prefix of b"#HELLO"
        keep = 0
        for k in range(5, 0, -1):
            if self._raw_buffer.endswith(b"#HELLO"[:k]):
                keep = k
                break

        if keep > 0:
            to_process = bytes(self._raw_buffer[:-keep])
            del self._raw_buffer[:-keep]
        else:
            to_process = bytes(self._raw_buffer)
            self._raw_buffer.clear()

        if to_process:
            result["lines"].extend(self._process_text_chunk(to_process))
        return result

    def _process_text_chunk(self, chunk: bytes) -> List[str]:
        text = chunk.decode("ascii", errors="replace")
        self._line_buffer += text

        # Prevent unbounded buffer growth if remote sends endless data without \r\n
        if len(self._line_buffer) > self._max_buffer_size:
            if "\n" in self._line_buffer:
                parts = self._line_buffer.replace("\r\n", "\n").split("\n")
                self._line_buffer = parts[-1][-self._max_buffer_size :]
                return [item.strip() for item in parts[:-1] if item.strip()]
            self._line_buffer = ""
            return []

        if "\r\n" in self._line_buffer or "\n" in self._line_buffer:
            normalized = self._line_buffer.replace("\r\n", "\n")
            parts = normalized.split("\n")
            self._line_buffer = parts[-1]  # Keep trailing incomplete line fragment
            return [line_item.strip() for line_item in parts[:-1] if line_item.strip()]

        return []

    def flush_remaining(self) -> List[str]:
        """Flushes any remaining incomplete line buffer."""
        if self._raw_buffer:
            self._line_buffer += bytes(self._raw_buffer).decode(
                "ascii", errors="replace"
            )
            self._raw_buffer.clear()
        if self._line_buffer.strip():
            rem = [self._line_buffer.strip()]
            self._line_buffer = ""
            return rem
        self._line_buffer = ""
        return []


def compute_aprs_passcode(callsign: str) -> int:
    """
    Computes the standard 16-bit APRS-IS validation passcode for an amateur callsign.
    Strips any SSID suffix (e.g. 'N0CALL-10' -> 'N0CALL').
    """
    base = callsign.strip().upper().split("-")[0]
    if not base:
        return -1
    h = 0x73E2
    for i in range(0, len(base), 2):
        h ^= ord(base[i]) << 8
        if i + 1 < len(base):
            h ^= ord(base[i + 1])
    return h & 0x7FFF


def parse_eeprom(raw: bytes) -> Dict[str, Any]:
    """
    Parses the 512-byte EEPROM payload returned after the 6-byte '#HELLO' prefix.
    Verified byte-by-byte against APRS 51TNC SETUP _ENGLISH_20140701.exe.
    """
    if len(raw) < 512:
        raise ValueError(f"Expected at least 512 bytes of EEPROM data, got {len(raw)}")

    buf = bytearray(raw[:512])

    def get_ascii(start: int, length: int, strip: bool = True) -> str:
        s = bytes(buf[start : start + length])
        s = s.split(b"\x00")[0]
        text = s.decode("ascii", errors="ignore")
        return text.strip() if strip else text

    def get_ip(start: int) -> str:
        return f"{buf[start]}.{buf[start + 1]}.{buf[start + 2]}.{buf[start + 3]}"

    def get_mac(start: int) -> str:
        return ":".join(f"{b:02X}" for b in buf[start : start + 6])

    def get_be_uint16(start: int) -> int:
        return (buf[start] * 256) + buf[start + 1]

    def path_idx_to_str(idx: int) -> str:
        return PATH_OPTIONS[idx] if 0 <= idx < len(PATH_OPTIONS) else PATH_OPTIONS[0]

    config = {
        # Station ID (0-15)
        "callsign": get_ascii(0, 6),
        "ssid": buf[7],
        "passcode": get_ascii(9, 6),
        # Network IP Settings (16-33)
        "ip_address": get_ip(16),
        "netmask": get_ip(20),
        "gateway": get_ip(24),
        "mac_address": get_mac(28),
        # APRS-IS Servers & Gateway Settings (34-127)
        "server_a": get_ip(34),
        "server_b": get_ip(38),
        "server_c": get_ip(42),
        "allow_is_to_rf": bool(buf[46]),
        "server_mode": "always_a" if buf[47] == 1 else "rotate",
        "timeout_restart": bool(buf[48]),
        "hellocq_enabled": bool(buf[49]),
        "lcd_type": LCD_TYPE_OPTIONS[min(buf[51], 2)] if buf[51] <= 2 else "Close",
        "lcd_type_code": buf[51],
        "pm25_enabled": bool(buf[52]),
        "aprs_filter": get_ascii(64, 64),
        # Gateway Beacon Settings (128-255)
        # Offsets 128-129 (IS) and 130-131 (RF) are Big-Endian uint16 (0-65535 sec)
        "gateway_beacon_is_sec": get_be_uint16(128),
        "gateway_beacon_rf_sec": get_be_uint16(130),
        "gateway_path_idx": buf[132],
        "gateway_path": path_idx_to_str(buf[132]),
        "gateway_report_voltage": bool(buf[133]),
        "gateway_ssid": buf[135],
        "gateway_packet_type": chr(buf[144]) if buf[144] > 0 else "!",
        "gateway_lat": get_ascii(145, 8),
        "gateway_symbol_table": chr(buf[153]) if buf[153] > 0 else "/",
        "gateway_lon": get_ascii(154, 9),
        "gateway_symbol_code": chr(buf[163]) if buf[163] > 0 else "&",
        "gateway_comment": get_ascii(164, 70, strip=False),
        # Weather Beacon Settings (256-383)
        # Offsets 256-257 (IS) and 258-259 (RF) are Big-Endian uint16
        "weather_beacon_is_sec": get_be_uint16(256),
        "weather_beacon_rf_sec": get_be_uint16(258),
        "weather_path_idx": buf[260],
        "weather_path": path_idx_to_str(buf[260]),
        "weather_report_voltage": bool(buf[261]),
        "weather_ssid": buf[263],
        "weather_use_gps_coords": bool(buf[264]),
        "weather_lcd_display": bool(buf[267]),
        "weather_packet_type": chr(buf[272]) if buf[272] > 0 else "=",
        "weather_lat": get_ascii(273, 8),
        "weather_symbol_table": chr(buf[281]) if buf[281] > 0 else "/",
        "weather_lon": get_ascii(282, 9),
        "weather_symbol_code": chr(buf[291]) if buf[291] > 0 else "_",
        "weather_telemetry_format": get_ascii(292, 33),
        "weather_comment": get_ascii(325, 50, strip=False),
        # Radio & Modem Hardware Settings (384-395)
        "ptt_delay_code": buf[
            384
        ],  # 0=250ms, 1=500ms, 2=750ms, 3=1000ms, 4=1250ms, 5=1500ms
        "min_tx_interval_code": buf[385],  # 0=250ms, 1=500ms, 2=750ms, 3=1000ms
        "tx_level": "ICOM_HIGH" if buf[387] == 1 else "YAESU_LOW",
        "baud_mode": BAUD_OPTIONS[buf[388] & 3],
        "digi_wide1": bool(buf[389]),
        "digi_wide2": bool(buf[390]),
        "insert_gateway_call": bool(buf[391]),
        "tx_waiting_code": buf[392],  # 0=250ms, 1=500ms, 2=750ms, 3=1000ms
        "wait_channel_idle": bool(buf[393]),
        "sql_hardware_detect": bool(buf[394]),
        "rf_lcd_display": bool(buf[395]),
        # GPS & Tracking Settings (396-492)
        "gps_baud": 9600 if buf[396] == 1 else 4800,
        "gps_lcd_display": bool(buf[397]),
        "gps_tracks": bool(buf[398]),
        "gps_beacon_interval": get_be_uint16(400),
        "gps_beacon_mode": GPS_MODE_OPTIONS[min(buf[402], 3)],
        "gps_ssid": buf[403],
        "gps_path_idx": buf[404],
        "gps_path": path_idx_to_str(buf[404]),
        "gps_packet_type": chr(buf[405]) if buf[405] > 0 else "!",
        "gps_symbol_table": chr(buf[406]) if buf[406] > 0 else "/",
        "gps_symbol_code": chr(buf[407]) if buf[407] > 0 else "#",
        "report_voltage": bool(buf[408]),
        "gps_offset_enabled": bool(buf[409]),
        "gps_lat_offset_neg": bool(buf[410]),
        "gps_lat_offset": buf[411],
        "gps_lon_offset_neg": bool(buf[412]),
        "gps_lon_offset": buf[413],
        "gps_custom_info": get_ascii(432, 60, strip=False),
    }
    return config


def build_eeprom(config: Dict[str, Any], template: Optional[bytes] = None) -> bytes:
    """
    Serializes a configuration dictionary back into the 512-byte EEPROM payload.
    Matches byte-for-byte with APRS 51TNC SETUP _ENGLISH_20140701.exe (Button6_Click).
    """
    buf = bytearray(template[:512] if template and len(template) >= 512 else 512)

    def set_ascii(start: int, length: int, text: str, strip: bool = True):
        raw_str = str(text or "")
        if strip:
            raw_str = raw_str.strip()
        b = raw_str.encode("ascii", errors="ignore")[:length]
        for i in range(length):
            buf[start + i] = b[i] if i < len(b) else 0

    def set_ip(start: int, ip_str: str):
        raw_ip = str(ip_str or "").strip()
        if not raw_ip:
            return
        parts = raw_ip.split(".")
        if len(parts) != 4:
            raise ValueError(f"Invalid IPv4 address: {ip_str!r}")
        for i, p in enumerate(parts):
            val = int(p)
            if not (0 <= val <= 255):
                raise ValueError(f"Invalid IPv4 octet {val} in {ip_str!r}")
            buf[start + i] = val

    def set_mac(start: int, mac_str: str):
        raw_mac = str(mac_str or "").strip()
        if not raw_mac:
            return
        parts = raw_mac.replace("-", ":").split(":")
        if len(parts) != 6:
            raise ValueError(f"Invalid MAC address: {mac_str!r}")
        for i, p in enumerate(parts):
            val = int(p, 16)
            if not (0 <= val <= 255):
                raise ValueError(f"Invalid MAC byte in {mac_str!r}")
            buf[start + i] = val

    def set_be_uint16(start: int, val: int):
        v = max(0, min(65535, int(val)))
        buf[start] = (v >> 8) & 0xFF
        buf[start + 1] = v & 0xFF

    def get_char_byte(val: Any, default: str) -> int:
        s = str(val) if val is not None and str(val) != "" else default
        if not s:
            return 0
        code = ord(s[0])
        return code if 0 <= code <= 127 else ord(default[0]) if default else 0

    def str_to_path_idx(path_val: Any) -> int:
        if isinstance(path_val, int):
            return min(2, max(0, path_val))
        s = str(path_val).strip()
        for idx, opt in enumerate(PATH_OPTIONS):
            if opt == s:
                return idx
        if "WIDE2" in s:
            return 2
        if "WIDE1" in s:
            return 1
        return 0

    # Station ID (0-15)
    set_ascii(0, 6, str(config.get("callsign", "NOCALL")).upper())
    buf[7] = max(0, min(15, int(config.get("ssid", 10)))) & 0xFF
    set_ascii(9, 6, str(config.get("passcode", "-1")))

    # Network (16-33)
    set_ip(16, config.get("ip_address", "192.168.1.120"))
    set_ip(20, config.get("netmask", "255.255.255.0"))
    set_ip(24, config.get("gateway", "192.168.1.1"))
    if "mac_address" in config and config["mac_address"]:
        set_mac(28, config["mac_address"])

    # APRS-IS Servers & Filter (34-127)
    set_ip(34, config.get("server_a", "218.104.177.211"))
    set_ip(38, config.get("server_b", "0.0.0.0"))
    set_ip(42, config.get("server_c", "0.0.0.0"))
    buf[46] = 1 if config.get("allow_is_to_rf") else 0
    buf[47] = 1 if config.get("server_mode") == "always_a" else 0
    buf[48] = 1 if config.get("timeout_restart") else 0
    buf[49] = 1 if config.get("hellocq_enabled") else 0

    lcd_type_val = config.get("lcd_type_code", config.get("lcd_type", 0))
    if isinstance(lcd_type_val, str):
        buf[51] = (
            1
            if "12864" in lcd_type_val or "ST7920" in lcd_type_val
            else (2 if "Color" in lcd_type_val else 0)
        )
    else:
        buf[51] = min(2, max(0, int(lcd_type_val)))

    buf[52] = 1 if config.get("pm25_enabled") else 0
    set_ascii(64, 64, config.get("aprs_filter", ""))

    # Gateway Beacons (128-255)
    set_be_uint16(128, int(config.get("gateway_beacon_is_sec", 120)))
    set_be_uint16(130, int(config.get("gateway_beacon_rf_sec", 0)))
    buf[132] = str_to_path_idx(
        config.get("gateway_path_idx", config.get("gateway_path", 1))
    )
    buf[133] = 1 if config.get("gateway_report_voltage") else 0
    buf[135] = (
        max(0, min(15, int(config.get("gateway_ssid", config.get("ssid", 10))))) & 0xFF
    )

    buf[144] = get_char_byte(config.get("gateway_packet_type"), "!")
    set_ascii(145, 8, config.get("gateway_lat", ""))
    buf[153] = get_char_byte(config.get("gateway_symbol_table"), "/")
    set_ascii(154, 9, config.get("gateway_lon", ""))
    buf[163] = get_char_byte(config.get("gateway_symbol_code"), "&")
    set_ascii(164, 70, config.get("gateway_comment", ""), strip=False)

    # Weather Beacons (256-383)
    set_be_uint16(256, int(config.get("weather_beacon_is_sec", 0)))
    set_be_uint16(258, int(config.get("weather_beacon_rf_sec", 0)))
    buf[260] = str_to_path_idx(
        config.get("weather_path_idx", config.get("weather_path", 1))
    )
    buf[261] = 1 if config.get("weather_report_voltage") else 0
    buf[263] = max(0, min(15, int(config.get("weather_ssid", 0)))) & 0xFF
    buf[264] = 1 if config.get("weather_use_gps_coords") else 0
    buf[267] = 1 if config.get("weather_lcd_display") else 0

    buf[272] = get_char_byte(config.get("weather_packet_type"), "=")
    set_ascii(273, 8, config.get("weather_lat", ""))
    buf[281] = get_char_byte(config.get("weather_symbol_table"), "/")
    set_ascii(282, 9, config.get("weather_lon", ""))
    buf[291] = get_char_byte(config.get("weather_symbol_code"), "_")

    default_telemetry = config.get(
        "weather_telemetry_format", "c000s000g000t050r000p000h50b10050"
    )
    set_ascii(292, 33, default_telemetry)
    set_ascii(325, 50, config.get("weather_comment", ""), strip=False)

    # Radio & Modem Hardware (384-395)
    buf[384] = max(0, min(5, int(config.get("ptt_delay_code", 1)))) & 0xFF
    buf[385] = max(0, min(3, int(config.get("min_tx_interval_code", 1)))) & 0xFF
    buf[387] = 1 if config.get("tx_level") == "ICOM_HIGH" else 0

    baud_map = {"Clever 4800": 0, "Text 9600": 1, "KISS 9600": 2, "Clever 9600": 3}
    buf[388] = baud_map.get(config.get("baud_mode", "Clever 9600"), 3)

    buf[389] = 1 if config.get("digi_wide1") else 0
    buf[390] = 1 if config.get("digi_wide2") else 0
    buf[391] = 1 if config.get("insert_gateway_call") else 0
    buf[392] = max(0, min(3, int(config.get("tx_waiting_code", 1)))) & 0xFF
    buf[393] = 1 if config.get("wait_channel_idle", True) else 0
    buf[394] = 1 if config.get("sql_hardware_detect") else 0
    buf[395] = 1 if config.get("rf_lcd_display", False) else 0

    # GPS (396-492)
    buf[396] = 1 if int(config.get("gps_baud", 9600)) == 9600 else 0
    buf[397] = 1 if config.get("gps_lcd_display") else 0
    buf[398] = 1 if config.get("gps_tracks") else 0
    set_be_uint16(400, int(config.get("gps_beacon_interval", 0)))

    mode_map = {"Close": 0, "Timing": 1, "SmartBeacon": 2, "Manual_IO2": 3}
    buf[402] = mode_map.get(config.get("gps_beacon_mode", "Close"), 0)
    buf[403] = max(0, min(15, int(config.get("gps_ssid", 0)))) & 0xFF
    buf[404] = str_to_path_idx(config.get("gps_path_idx", config.get("gps_path", 0)))
    buf[405] = get_char_byte(config.get("gps_packet_type"), "!")
    buf[406] = get_char_byte(config.get("gps_symbol_table"), "/")
    buf[407] = get_char_byte(config.get("gps_symbol_code"), "#")
    buf[408] = 1 if config.get("report_voltage") else 0
    buf[409] = 1 if config.get("gps_offset_enabled") else 0
    buf[410] = 1 if config.get("gps_lat_offset_neg") else 0
    buf[411] = max(0, min(255, int(config.get("gps_lat_offset", 0)))) & 0xFF
    buf[412] = 1 if config.get("gps_lon_offset_neg") else 0
    buf[413] = max(0, min(255, int(config.get("gps_lon_offset", 0)))) & 0xFF
    set_ascii(432, 60, config.get("gps_custom_info", ""), strip=False)

    return bytes(buf)


def parse_weather_telemetry(raw: str) -> Optional[Dict[str, Any]]:
    """
    Parses standard APRS raw weather telemetry string:
    'c000s000g000t050r000p000h50b10050' (also supports negative temps like t-05,
    missing fields with '...' or spaces, and APRS h00 = 100% humidity).
    c: wind direction (deg)
    s: wind speed sustained 1-min (mph)
    g: wind gust 5-min peak (mph)
    t: temperature (deg F, e.g. 050 or -05)
    r: rainfall last 1 hour (hundredths of an inch)
    p: rainfall last 24 hours (hundredths of an inch)
    h: humidity (%, 00 = 100%)
    b: barometric pressure (tenths of hPa, e.g. 10132 = 1013.2 hPa)
    """
    m = re.search(
        r"c(\d{3}|\.{3}|   )s(\d{3}|\.{3}|   )g(\d{3}|\.{3}|   )t(-?\d{2,3}|\.{3}|   )"
        r"(?:r(\d{3}|\.{3}|   ))?(?:p(\d{3}|\.{3}|   ))?(?:P(\d{3}|\.{3}|   ))?"
        r"h(\d{2}|\.{2}|  )b(\d{4,5}|\.{4,5}|     )",
        raw,
    )
    if not m:
        return None

    c, s, g, t, r, p, _p_mid, h, b = m.groups()

    def _int_or_zero(val: Optional[str]) -> int:
        if not val or "." in val or not val.strip():
            return 0
        return int(val.strip())

    try:
        wind_dir = _int_or_zero(c)
        wind_speed_mph = _int_or_zero(s)
        wind_speed_ms = round(wind_speed_mph * 0.44704, 1)
        wind_gust_mph = _int_or_zero(g)
        wind_gust_ms = round(wind_gust_mph * 0.44704, 1)
        temp_f = _int_or_zero(t)
        temp_c = round((temp_f - 32) * 5.0 / 9.0, 1)
        rain_1h_in = _int_or_zero(r) * 0.01
        rain_1h_mm = round(rain_1h_in * 25.4, 1)
        rain_24h_in = _int_or_zero(p) * 0.01
        rain_24h_mm = round(rain_24h_in * 25.4, 1)
        humidity = _int_or_zero(h)
        if h and h.strip() == "00":
            humidity = 100
        pressure_raw = _int_or_zero(b)
        # In APRS, 'b' is 5 digits in tenths of hPa (e.g. 10132 -> 1013.2 hPa, 09980 -> 998.0 hPa)
        pressure_hpa = (
            round(pressure_raw / 10.0, 1)
            if pressure_raw >= 5000
            else float(pressure_raw)
        )

        return {
            "wind_direction_deg": wind_dir,
            "wind_speed_mph": wind_speed_mph,
            "wind_speed_ms": wind_speed_ms,
            "wind_gust_mph": wind_gust_mph,
            "wind_gust_ms": wind_gust_ms,
            "temp_f": temp_f,
            "temp_c": temp_c,
            "rain_1h_mm": rain_1h_mm,
            "rain_24h_mm": rain_24h_mm,
            "humidity_pct": humidity,
            "pressure_hpa": pressure_hpa,
            "raw": raw,
        }
    except (ValueError, TypeError):
        return None


# APRS compressed-position payload starts with a symbol-table identifier
# (/ or \ or a GIS overlay char A-Z / 0-9) followed by 4 base-91 lat chars,
# 4 base-91 lon chars, a symbol code, and 3 cs/compression bytes.
# Reference: APRS 1.01 spec, "Compressed Position Report Formats".
_COMPRESSED_RE = re.compile(
    r"^([\/\\A-Za-j])([\x21-\x7b]{4})([\x21-\x7b]{4})([\x21-\x7b])([\x21-\x7b]{2})([\x21-\x7b\x7d])(.*)",
    re.DOTALL,
)


def _b91_decode(chars: str) -> int:
    """Decode a base-91 (printable-ASCII, offset 33) APRS value."""
    value = 0
    for ch in chars:
        value = value * 91 + (ord(ch) - 33)
    return value


def parse_compressed_position(payload: str) -> Optional[Dict[str, Any]]:
    """Decode an APRS compressed position report.

    Compressed positions pack lat/lon into base-91 and are silently dropped by
    the uncompressed regex. Returns a dict with lat/lon/symbol or None if the
    payload is not a compressed position.
    """
    if not payload or payload[:1] not in ("=", "!", "@", "/"):
        return None
    dti = payload[0]
    body = payload[1:]
    if dti in ("@", "/"):
        if len(body) < 7:
            return None
        body = body[7:]

    m = _COMPRESSED_RE.match(body)
    if not m:
        return None
    sym_table, lat_raw, lon_raw, sym_code, _cs, _comp, comment = m.groups()

    try:
        yy = _b91_decode(lat_raw)
        xx = _b91_decode(lon_raw)
        lat = 90.0 - yy / 380926.0
        lon = -180.0 + xx / 190463.0
    except (ValueError, TypeError):
        return None

    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None

    return {
        "lat": round(lat, 5),
        "lon": round(lon, 5),
        "symbol_table": sym_table,
        "symbol_code": sym_code,
        "comment": comment.strip(),
        "compressed": True,
    }


def parse_mice_position(dest: str, payload: str) -> Optional[Dict[str, Any]]:
    """
    Decode an APRS Mic-E position report (used by Yaesu/Kenwood transceivers).
    Destination callsign (6 chars) encodes latitude, N/S, E/W, and longitude offset;
    payload starts with ` or ' followed by 3 longitude bytes, 3 speed/course bytes,
    symbol code, and symbol table.
    """
    if not payload or payload[0] not in ("`", "'") or len(payload) < 9:
        return None
    dest_call = dest.split("-")[0].upper()
    if len(dest_call) < 6:
        return None

    lat_digits = []
    for ch in dest_call[:6]:
        if "0" <= ch <= "9":
            lat_digits.append(ch)
        elif "A" <= ch <= "J":
            lat_digits.append(chr(ord(ch) - ord("A") + ord("0")))
        elif "P" <= ch <= "Y":
            lat_digits.append(chr(ord(ch) - ord("P") + ord("0")))
        elif ch in ("K", "L", "Z"):
            lat_digits.append(" ")
        else:
            return None

    lat_str = "".join(lat_digits).replace(" ", "0")
    try:
        lat_deg = int(lat_str[:2])
        lat_min = int(lat_str[2:4]) + int(lat_str[4:6]) / 100.0
        lat = lat_deg + lat_min / 60.0
    except ValueError:
        return None

    # Destination char 4 (index 3): N if >= 'P', else S
    if dest_call[3] < "P":
        lat = -lat

    # Destination char 5 (index 4): +100 deg lon offset if >= 'P'
    lon_offset = 100 if dest_call[4] >= "P" else 0
    # Destination char 6 (index 5): W if >= 'P', else E
    is_west = dest_call[5] >= "P"

    d = ord(payload[1]) - 28 + lon_offset
    if 180 <= d <= 189:
        d -= 80
    elif 190 <= d <= 199:
        d -= 190

    m_lon = ord(payload[2]) - 28
    if m_lon >= 60:
        m_lon -= 60
    h_lon = ord(payload[3]) - 28

    lon = d + (m_lon + h_lon / 100.0) / 60.0
    if is_west:
        lon = -lon

    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None

    sym_code = payload[7]
    sym_table = payload[8]
    comment = payload[9:].strip()

    return {
        "lat": round(lat, 5),
        "lon": round(lon, 5),
        "symbol_table": sym_table,
        "symbol_code": sym_code,
        "comment": comment,
        "mice": True,
    }


# Uncompressed position regex anchored to standard APRS position DTIs (!, =, /, @) or object/item (;)).
_UNCOMPRESSED_POS_RE = re.compile(
    r"^(?:[!=]|[@/].{7}|;.{9}[*_].{7}|\).{3,9}[!_])"
    r"(\d{2})([0-5 ][0-9 ]\.[0-9 ]{2})([NS])(.)(\d{3})([0-5 ][0-9 ]\.[0-9 ]{2})([EW])(.)(.*)",
    re.DOTALL,
)


# NET-51TNC hardware stream direction prefixes emitted on TCP port 60000
_TNC_STREAM_PREFIX_RE = re.compile(
    r"^(RF\s*->\s*TNC|TNC\s*->\s*IS|DIGI\s*->\s*RF|GATE\s*->\s*IS|GATE\s*->\s*RF|"
    r"WX\s*->\s*IS|WX\s*->\s*RF|GPS\s*->\s*RF|GPS\s*->\s*IS|IS\s*->\s*TNC|IS\s*->\s*RF)\s*:\s*",
    re.IGNORECASE,
)


def parse_aprs_packet(line: str) -> Optional[Dict[str, Any]]:
    """
    Parses a single raw APRS line streamed by the NET51TNC.
    Handles RF packets (including 'RF ->TNC :' hardware prefixes),
    APRS-IS packets (prefixed with '# ' or 'IS ->TNC :'), local TNC beacons
    ('GATE ->IS:', 'GATE ->RF:', etc.), and ignores serial output logs ('TTL->GARMIN:').
    """
    raw = line.strip()
    if not raw:
        return None

    # Ignore non-APRS TTL serial port output logs (e.g. "TTL->GARMIN: $GPWPL,...")
    if re.match(r"^TTL\s*->", raw, re.IGNORECASE):
        return {
            "is_packet": False,
            "raw": line,
            "is_server": False,
            "weather": None,
        }

    is_server = False
    if raw.startswith("# "):
        is_server = True
        raw = raw[2:].strip()
        if raw.lower().startswith("aprsc ") or raw.lower().startswith("javaprssrvr"):
            return {
                "is_packet": False,
                "raw": line,
                "is_server": True,
                "weather": None,
            }

    tnc_tag = None
    prefix_match = _TNC_STREAM_PREFIX_RE.match(raw)
    if prefix_match:
        tnc_tag = re.sub(r"\s+", "", prefix_match.group(1)).upper()
        raw = raw[prefix_match.end() :].strip()
        if tnc_tag in ("IS->TNC", "IS->RF", "GATE->IS", "WX->IS", "GPS->IS"):
            is_server = True
        elif tnc_tag in (
            "RF->TNC",
            "TNC->IS",
            "DIGI->RF",
            "GATE->RF",
            "WX->RF",
            "GPS->RF",
        ):
            is_server = False

    # Match packet header: CALLSIGN[-SSID]>DESTINATION,PATH:PAYLOAD
    # Callsign and destination must not end with a hyphen (prevents matching "FOO->BAR:")
    header_match = re.match(
        r"^([A-Za-z0-9]{1,6}(?:-[A-Za-z0-9]{1,2})?)>([A-Za-z0-9]{1,6}(?:-[A-Za-z0-9]{1,2})?)(?:,([^:]+))?:(.*)$",
        raw,
    )
    if not header_match:
        # Status / telemetry line (voltage, server login, or standalone weather)
        return {
            "is_packet": False,
            "raw": line,
            "is_server": is_server,
            "weather": parse_weather_telemetry(raw),
        }

    source, dest, path, payload = header_match.groups()
    packet = {
        "is_packet": True,
        "is_server": is_server,
        "tnc_tag": tnc_tag,
        "source": source.upper(),
        "dest": dest.upper(),
        "path": path or "",
        "payload": payload,
        "raw": line,
        "lat": None,
        "lon": None,
        "symbol_table": "/",
        "symbol_code": "",
        "comment": "",
        "weather": parse_weather_telemetry(payload),
    }

    # 1. Extract coordinates: standard APRS uncompressed position (with ambiguity spaces handled)
    coord_match = _UNCOMPRESSED_POS_RE.match(payload)
    if coord_match:
        lat_deg, lat_min, lat_ns, table, lon_deg, lon_min, lon_ew, sym, comment = (
            coord_match.groups()
        )
        try:
            lat = float(lat_deg) + float(lat_min.replace(" ", "0")) / 60.0
            if lat_ns == "S":
                lat = -lat
            lon = float(lon_deg) + float(lon_min.replace(" ", "0")) / 60.0
            if lon_ew == "W":
                lon = -lon

            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                packet["lat"] = round(lat, 5)
                packet["lon"] = round(lon, 5)
                packet["symbol_table"] = table
                packet["symbol_code"] = sym
                packet["comment"] = comment.strip()
        except ValueError:
            logger.debug("Malformed uncompressed coordinate in packet: %s", line)

    # 2. Fall back to compressed (base-91) position
    if packet["lat"] is None:
        compressed = parse_compressed_position(payload)
        if compressed:
            packet["lat"] = compressed["lat"]
            packet["lon"] = compressed["lon"]
            packet["symbol_table"] = compressed["symbol_table"]
            packet["symbol_code"] = compressed["symbol_code"]
            packet["comment"] = compressed["comment"]
            packet["compressed"] = True

    # 3. Fall back to Mic-E position
    if packet["lat"] is None:
        mice = parse_mice_position(dest, payload)
        if mice:
            packet["lat"] = mice["lat"]
            packet["lon"] = mice["lon"]
            packet["symbol_table"] = mice["symbol_table"]
            packet["symbol_code"] = mice["symbol_code"]
            packet["comment"] = mice["comment"]
            packet["mice"] = True

    return packet
