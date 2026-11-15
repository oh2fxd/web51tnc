"""
FastAPI Server & Web UI for NET51TNC APRS Gateway / Digipeater
"""

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
from typing import Dict, Any, List, Optional, Union

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from net51tnc_protocol import (
    parse_eeprom,
    build_eeprom,
    compute_aprs_passcode,
    parse_aprs_packet,
    parse_weather_telemetry,
    compute_station_freshness,
    aprs_coord_to_decimal,
    haversine_km,
    bearing_deg,
    ConnectionState,
    ConfigSyncState,
    StationFreshness,
    StreamFramer,
    StreamFramerState,
    CMD_READ,
    CMD_RESET,
    CMD_INSTALL,
    CMD_GATE_RF_TEST,
    CMD_GATE_IS_TEST,
    CMD_WX_RF_TEST,
    CMD_WX_IS_TEST,
    DEFAULT_PORT,
    DEFAULT_IP,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("web51tnc")

MAX_HEARD_STATIONS = int(os.environ.get("MAX_HEARD_STATIONS", "500"))
MAX_LONGEST_STATIONS = int(os.environ.get("MAX_LONGEST_STATIONS", "50"))
MAX_STATION_AGE_SEC = int(
    os.environ.get("MAX_STATION_AGE_SEC", "7200")
)  # 2 hours live map retention
DEFAULT_BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
RAM_STATIONS_PATH = (
    "/dev/shm/web51tnc_stations.json"
    if os.path.isdir("/dev/shm")
    else "/tmp/web51tnc_stations.json"
)
VOLTAGE_RE = re.compile(r"^\d{1,2}\.\d{1,2}V$", re.IGNORECASE)
IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
CALLSIGN_RE = re.compile(r"^[A-Z0-9]{1,6}$")


def verify_api_token(x_api_token: Optional[str] = Header(default=None)):
    """Optional API token guard configured via WEB51TNC_TOKEN environment variable."""
    expected = os.environ.get("WEB51TNC_TOKEN", "").strip()
    if expected and x_api_token != expected:
        raise HTTPException(
            status_code=401, detail="Invalid or missing X-API-Token header"
        )


class ConnectRequest(BaseModel):
    ip: str = DEFAULT_IP
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)


class WifiConnectRequest(BaseModel):
    ssid: str = Field(..., min_length=1, max_length=64)
    password: str = Field(default="", max_length=128)


class WifiApRequest(BaseModel):
    action: str = Field(default="start", pattern="^(start|stop)$")


class TNCConfigModel(BaseModel):
    """Validated EEPROM configuration schema for NET-51TNC."""

    model_config = ConfigDict(extra="allow")

    callsign: str = Field(default="NOCALL", min_length=1, max_length=6)
    ssid: int = Field(default=10, ge=0, le=15)
    passcode: Union[str, int] = Field(default="-1")

    ip_address: str = Field(default="192.168.1.120")
    netmask: str = Field(default="255.255.255.0")
    gateway: str = Field(default="192.168.1.1")
    mac_address: Optional[str] = None

    server_a: str = Field(default="218.104.177.211")
    server_b: str = Field(default="0.0.0.0")
    server_c: str = Field(default="0.0.0.0")
    server_mode: str = Field(default="rotate")
    aprs_filter: str = Field(default="", max_length=64)
    lcd_type: Union[str, int] = Field(default="Close")
    lcd_type_code: Optional[int] = Field(default=None, ge=0, le=2)
    allow_is_to_rf: bool = False
    timeout_restart: bool = True
    hellocq_enabled: bool = False
    pm25_enabled: bool = False

    baud_mode: str = Field(default="Clever 9600")
    tx_level: str = Field(default="YAESU_LOW")
    ptt_delay_code: int = Field(default=1, ge=0, le=5)
    min_tx_interval_code: int = Field(default=1, ge=0, le=3)
    tx_waiting_code: int = Field(default=1, ge=0, le=3)
    digi_wide1: bool = False
    digi_wide2: bool = False
    insert_gateway_call: bool = False
    wait_channel_idle: bool = True
    sql_hardware_detect: bool = False
    rf_lcd_display: bool = False

    gateway_beacon_is_sec: int = Field(default=120, ge=0, le=65535)
    gateway_beacon_rf_sec: int = Field(default=0, ge=0, le=65535)
    gateway_path: Union[str, int] = Field(default="WIDE1-1")
    gateway_path_idx: Optional[int] = Field(default=None, ge=0, le=2)
    gateway_ssid: int = Field(default=10, ge=0, le=15)
    gateway_packet_type: str = Field(default="!", max_length=1)
    gateway_lat: str = Field(default="", max_length=8)
    gateway_symbol_table: str = Field(default="/", max_length=1)
    gateway_lon: str = Field(default="", max_length=9)
    gateway_symbol_code: str = Field(default="&", max_length=1)
    gateway_comment: str = Field(default="", max_length=70)
    gateway_report_voltage: bool = False

    weather_beacon_is_sec: int = Field(default=0, ge=0, le=65535)
    weather_beacon_rf_sec: int = Field(default=0, ge=0, le=65535)
    weather_path: Union[str, int] = Field(default="WIDE1-1")
    weather_path_idx: Optional[int] = Field(default=None, ge=0, le=2)
    weather_ssid: int = Field(default=13, ge=0, le=15)
    weather_packet_type: str = Field(default="=", max_length=1)
    weather_lat: str = Field(default="", max_length=8)
    weather_symbol_table: str = Field(default="/", max_length=1)
    weather_lon: str = Field(default="", max_length=9)
    weather_symbol_code: str = Field(default="_", max_length=1)
    weather_telemetry_format: str = Field(
        default="c000s000g000t050r000p000h50b10050", max_length=33
    )
    weather_comment: str = Field(default="", max_length=50)
    weather_report_voltage: bool = False
    weather_use_gps_coords: bool = False
    weather_lcd_display: bool = False

    gps_baud: int = Field(default=4800)
    gps_beacon_interval: int = Field(default=0, ge=0, le=65535)
    gps_beacon_mode: str = Field(default="Close")
    gps_ssid: int = Field(default=9, ge=0, le=15)
    gps_path: Union[str, int] = Field(default="- - -")
    gps_path_idx: Optional[int] = Field(default=None, ge=0, le=2)
    gps_packet_type: str = Field(default="!", max_length=1)
    gps_symbol_table: str = Field(default="/", max_length=1)
    gps_symbol_code: str = Field(default="#", max_length=1)
    report_voltage: bool = False
    gps_tracks: bool = False
    gps_lcd_display: bool = False
    gps_offset_enabled: bool = False
    gps_lat_offset_neg: bool = False
    gps_lat_offset: int = Field(default=0, ge=0, le=255)
    gps_lon_offset_neg: bool = False
    gps_lon_offset: int = Field(default=0, ge=0, le=255)
    gps_custom_info: str = Field(default="", max_length=60)

    @field_validator("callsign")
    @classmethod
    def validate_callsign(cls, v: str) -> str:
        clean = v.strip().upper()
        if not CALLSIGN_RE.match(clean):
            raise ValueError(
                f"Invalid amateur callsign (1-6 alphanumeric characters): {v!r}"
            )
        return clean

    @field_validator(
        "ip_address", "netmask", "gateway", "server_a", "server_b", "server_c"
    )
    @classmethod
    def validate_ipv4(cls, v: str) -> str:
        clean = v.strip()
        try:
            ipaddress.IPv4Address(clean)
        except ipaddress.AddressValueError as exc:
            raise ValueError(f"Invalid IPv4 address: {v!r}") from exc
        return clean


class TNCManager:
    def __init__(self):
        self.ip = os.environ.get("TNC_IP", DEFAULT_IP)
        self.port = int(os.environ.get("TNC_PORT", str(DEFAULT_PORT)))
        self.auto_reconnect = os.environ.get("AUTO_CONNECT", "1").lower() in (
            "1",
            "true",
            "yes",
        )
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self._io_lock = asyncio.Lock()

        # Explicit State Machines (Connection & Lifecycle, EEPROM Sync, Stream Framer)
        self.state: ConnectionState = ConnectionState.DISCONNECTED
        self.config_state: ConfigSyncState = ConfigSyncState.UNKNOWN
        self.reboot_seconds_remaining: Optional[int] = None
        self.reboot_task: Optional[asyncio.Task] = None
        self.framer: StreamFramer = StreamFramer()

        # Backward compatibility attributes
        self.connected = False
        self.connecting = False

        self.listen_task: Optional[asyncio.Task] = None
        self.active_websockets: List[WebSocket] = []
        self.heard_stations: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self.longest_stations: Dict[str, Dict[str, Any]] = {}
        self.last_config: Optional[Dict[str, Any]] = None
        self.raw_template: Optional[bytes] = None
        self.backup_dir: str = os.environ.get("WEB51TNC_BACKUP_DIR", DEFAULT_BACKUP_DIR)
        self.latest_weather: Optional[Dict[str, Any]] = None
        self.latest_status: Dict[str, Any] = {
            "voltage": None,
            "server_ip": None,
            "server_port": None,
            "server_status": None,
        }
        self._config_event = asyncio.Event()
        self._load_persisted_state()

    async def _close_writer(self):
        if self.writer:
            writer = self.writer
            self.writer = None
            self.reader = None
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
            except (OSError, asyncio.TimeoutError) as ex:
                logger.debug("Socket close warning: %s", ex)

    async def transition_state(self, new_state: ConnectionState, reason: str = ""):
        old_state = self.state
        if old_state == new_state and self.reboot_seconds_remaining is None:
            return
        self.state = new_state
        self.connected = new_state == ConnectionState.CONNECTED
        self.connecting = new_state == ConnectionState.CONNECTING
        logger.info(
            "[State Machine] Connection: %s -> %s (reason: %s)",
            old_state.value,
            new_state.value,
            reason,
        )

        event_payload = {
            "type": "state_change",
            "state": self.state.value,
            "previous_state": old_state.value,
            "config_state": self.config_state.value,
            "reason": reason,
            "reboot_seconds": self.reboot_seconds_remaining,
            "connected": self.connected,
            "connecting": self.connecting,
            "ip": self.ip,
            "port": self.port,
        }
        await self.broadcast(event_payload)
        await self.broadcast(
            {
                "type": "connection",
                "connected": self.connected,
                "connecting": self.connecting,
                "state": self.state.value,
                "ip": self.ip,
                "port": self.port,
            }
        )

    def trigger_reboot_lifecycle(self, reason: str, wait_seconds: int = 8):
        """
        Initiates the hardware reboot supervisor state machine.
        Gracefully handles the transient TCP drop and auto-reconnects when boot completes.
        """
        if self.reboot_task and not self.reboot_task.done():
            self.reboot_task.cancel()
        self.reboot_task = asyncio.create_task(
            self._reboot_supervisor(reason, wait_seconds)
        )

    async def _reboot_supervisor(self, reason: str, wait_seconds: int):
        logger.info(
            "[State Machine] Reboot supervisor initiated: %s (est. %ds)",
            reason,
            wait_seconds,
        )
        await self.transition_state(ConnectionState.REBOOTING, reason)

        if self.listen_task:
            self.listen_task.cancel()
            self.listen_task = None

        # Keep TCP socket open during the first 3 seconds so the 8051 MCU on the
        # NET-51TNC has time to read all 518 EEPROM bytes from its W5100 buffer,
        # program flash, and trigger its own hardware reset before we close our side.
        hold_socket_sec = min(3, max(1, wait_seconds - 1))

        try:
            for elapsed, remaining in enumerate(range(wait_seconds, 0, -1)):
                if elapsed == hold_socket_sec:
                    await self._close_writer()
                self.reboot_seconds_remaining = remaining
                await self.broadcast(
                    {
                        "type": "state_change",
                        "state": ConnectionState.REBOOTING.value,
                        "reason": reason,
                        "reboot_seconds": remaining,
                        "connected": False,
                        "connecting": False,
                        "ip": self.ip,
                        "port": self.port,
                    }
                )
                await asyncio.sleep(1.0)
        finally:
            await self._close_writer()
            self.reboot_seconds_remaining = None

        logger.info(
            "[State Machine] Reboot countdown complete. Auto-reconnecting to NET51TNC..."
        )
        success = await self.try_connect(self.ip, self.port)
        if success:
            await asyncio.sleep(1.0)
            try:
                await self.read_config()
            except HTTPException as ex:
                logger.debug("Config poll after reboot failed: %s", ex.detail)
        else:
            if self.config_state == ConfigSyncState.SAVING:
                self.config_state = ConfigSyncState.UNKNOWN
                await self.broadcast(
                    {
                        "type": "state_change",
                        "state": self.state.value,
                        "config_state": self.config_state.value,
                        "reason": "reconnect_after_reboot_failed",
                        "connected": False,
                        "connecting": False,
                        "ip": self.ip,
                        "port": self.port,
                    }
                )

    async def try_connect(
        self, ip: Optional[str] = None, port: Optional[int] = None
    ) -> bool:
        if (
            self.state == ConnectionState.REBOOTING
            and self.reboot_seconds_remaining
            and self.reboot_seconds_remaining > 0
        ):
            logger.info("Connection attempt deferred: TNC is in reboot sequence.")
            return False

        if self.connected:
            await self.disconnect()

        if ip:
            self.ip = ip
        if port:
            self.port = port

        await self.transition_state(ConnectionState.CONNECTING, "initiating_socket")
        try:
            logger.info("Connecting to NET51TNC at %s:%d...", self.ip, self.port)
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.ip, self.port), timeout=5.0
            )
            sock = self.writer.get_extra_info("socket")
            if sock is not None:
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass
            self.framer = StreamFramer()
            self.listen_task = asyncio.create_task(self._listen_loop())
            logger.info("Connected to NET51TNC at %s:%d", self.ip, self.port)
            await self.transition_state(ConnectionState.CONNECTED, "socket_opened")
            return True
        except (OSError, asyncio.TimeoutError) as e:
            logger.warning(
                "Connection to NET51TNC at %s:%d failed: %s", self.ip, self.port, e
            )
            await self.transition_state(
                ConnectionState.DISCONNECTED, f"connection_failed: {e}"
            )
            return False

    async def connect(self, ip: str, port: int = DEFAULT_PORT):
        success = await self.try_connect(ip, port)
        if not success:
            raise HTTPException(
                status_code=500, detail=f"Failed to connect to {ip}:{port}"
            )
        return True

    async def auto_reconnect_loop(self):
        while True:
            if (
                self.state
                not in (
                    ConnectionState.CONNECTED,
                    ConnectionState.CONNECTING,
                    ConnectionState.REBOOTING,
                )
                and self.auto_reconnect
            ):
                logger.info(
                    "Auto-connect daemon attempting link to %s:%d...",
                    self.ip,
                    self.port,
                )
                success = await self.try_connect(self.ip, self.port)
                if success:
                    await asyncio.sleep(1.0)
                    try:
                        await self.read_config()
                    except HTTPException as ex:
                        logger.debug(
                            "Initial config poll after auto-connect: %s", ex.detail
                        )
            await asyncio.sleep(10.0)

    async def disconnect(self):
        if self.reboot_task and not self.reboot_task.done():
            self.reboot_task.cancel()
            self.reboot_task = None
        if self.listen_task:
            self.listen_task.cancel()
            self.listen_task = None
        await self._close_writer()
        logger.info("Disconnected from NET51TNC")
        await self.transition_state(ConnectionState.DISCONNECTED, "manual_disconnect")

    def _parse_status_text(self, text: str) -> bool:
        """
        Parses TNC status/telemetry lines strictly:
        - Voltage lines like '12.6V' or '13.8V'
        - Multi-line status dumps after #HELLO
        - Explicit server connection banners
        Returns True if any status field was updated.
        """
        updated = False
        lines = [
            line.strip()
            for line in text.replace("\r\n", "\n").split("\n")
            if line.strip()
        ]
        if not lines:
            return False

        # Multi-line status block format: [voltage, server_ip, server_port, status_code]
        if len(lines) >= 4 and VOLTAGE_RE.match(lines[0]) and IPV4_RE.match(lines[1]):
            self.latest_status["voltage"] = lines[0].upper()
            self.latest_status["server_ip"] = lines[1]
            self.latest_status["server_port"] = lines[2]
            code = lines[3]
            self.latest_status["server_status"] = (
                "Connected / Login OK" if code == "5" else f"Status {code}"
            )
            updated = True

        for line in lines:
            if VOLTAGE_RE.match(line):
                self.latest_status["voltage"] = line.upper()
                updated = True
            elif line.startswith("Server is connected") or line.startswith("Login OK"):
                self.latest_status["server_status"] = "Connected / Login OK"
                updated = True
            elif line.startswith("# aprsc") or line.startswith("# javAPRSSrvr"):
                # Example: "# aprsc 3.0.1-g79fe075 15 Nov 2026 03:58:23 GMT T2PRT 193.137.237.12:14580"
                m_srv = re.search(
                    r"(?:(\b[A-Z0-9_]{3,12})\s+)?(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})\b",
                    line,
                )
                if m_srv:
                    srv_name, srv_ip, srv_port = m_srv.groups()
                    self.latest_status["server_ip"] = srv_ip
                    self.latest_status["server_port"] = srv_port
                    self.latest_status["server_status"] = (
                        f"Connected ({srv_name})"
                        if srv_name
                        else "Connected / Login OK"
                    )
                else:
                    self.latest_status["server_status"] = "Connected / Login OK"
                updated = True
            elif IPV4_RE.match(line):
                self.latest_status["server_ip"] = line
                updated = True

        return updated

    def _prune_expired_stations(self, now_dt: Optional[datetime] = None) -> bool:
        """Removes stations older than MAX_STATION_AGE_SEC (default 2 hours)."""
        if now_dt is None:
            now_dt = datetime.now(timezone.utc)
        expired = []
        for call, st in list(self.heard_stations.items()):
            _, age_sec = compute_station_freshness(st.get("last_heard"), now_dt)
            if age_sec > MAX_STATION_AGE_SEC:
                expired.append(call)
        for call in expired:
            self.heard_stations.pop(call, None)
        return len(expired) > 0

    def _record_station(self, src: str, station: Dict[str, Any]):
        """Maintains a bounded 2-hour LRU of heard stations and persists to disk."""
        self._prune_expired_stations()
        if src in self.heard_stations:
            self.heard_stations.move_to_end(src)
        self.heard_stations[src] = station
        while len(self.heard_stations) > MAX_HEARD_STATIONS:
            self.heard_stations.popitem(last=False)
        self._save_heard_stations()

    def _save_heard_stations(self):
        """Persists live stations in RAM tmpfs (/dev/shm or /tmp) to avoid SD/USB flash wear."""
        try:
            with open(RAM_STATIONS_PATH, "w", encoding="utf-8") as f:
                json.dump(list(self.heard_stations.values()), f)
        except OSError as ex:
            logger.debug("Could not persist heard_stations to RAM tmpfs: %s", ex)

    def _load_persisted_state(self):
        """Restores last known EEPROM config from disk and live stations from RAM tmpfs."""
        try:
            latest_bin = os.path.join(self.backup_dir, "eeprom_latest.bin")
            if os.path.isfile(latest_bin):
                with open(latest_bin, "rb") as f:
                    raw = f.read(512)
                if len(raw) == 512:
                    self.raw_template = raw
                    self.last_config = parse_eeprom(raw)
                    self.config_state = ConfigSyncState.SYNCED
        except (OSError, ValueError) as ex:
            logger.debug("Could not restore cached EEPROM config: %s", ex)

        try:
            if os.path.isfile(RAM_STATIONS_PATH):
                with open(RAM_STATIONS_PATH, "r", encoding="utf-8") as f:
                    items = json.load(f)
                if isinstance(items, list):
                    now_dt = datetime.now(timezone.utc)
                    for st in items[-MAX_HEARD_STATIONS:]:
                        call = st.get("callsign", "")
                        if not call or call.endswith("-"):
                            continue
                        _, age_sec = compute_station_freshness(
                            st.get("last_heard"), now_dt
                        )
                        if age_sec <= MAX_STATION_AGE_SEC:
                            self.heard_stations[call] = st
        except (OSError, ValueError) as ex:
            logger.debug("Could not restore persisted stations: %s", ex)

        try:
            longest_path = self._longest_stations_path()
            if os.path.isfile(longest_path):
                with open(longest_path, "r", encoding="utf-8") as f:
                    items = json.load(f)
                if isinstance(items, list):
                    for entry in items:
                        if not isinstance(entry, dict):
                            continue
                        call = entry.get("callsign", "")
                        dist = entry.get("max_distance_km")
                        if not call or dist is None:
                            continue
                        key = f"{call}|{'IS' if entry.get('is_server') else 'RF'}"
                        self.longest_stations[key] = entry
        except (OSError, ValueError) as ex:
            logger.debug("Could not restore longest stations history: %s", ex)

    def _longest_stations_path(self) -> str:
        return os.path.join(self.backup_dir, "longest_stations.json")

    def _get_gateway_coords(self) -> Optional[tuple[float, float]]:
        if not self.last_config:
            return None
        lat = aprs_coord_to_decimal(
            self.last_config.get("gateway_lat", ""), is_lon=False
        )
        lon = aprs_coord_to_decimal(
            self.last_config.get("gateway_lon", ""), is_lon=True
        )
        if lat is None or lon is None:
            return None
        return (lat, lon)

    def _enrich_station_distance(
        self,
        station: Dict[str, Any],
        gw_coords: Optional[tuple[float, float]] = None,
    ) -> None:
        if gw_coords is None:
            gw_coords = self._get_gateway_coords()
        if gw_coords is None:
            return
        lat = station.get("lat")
        lon = station.get("lon")
        if lat is None or lon is None:
            return
        station["distance_km"] = haversine_km(
            gw_coords[0], gw_coords[1], float(lat), float(lon)
        )
        station["bearing_deg"] = bearing_deg(
            gw_coords[0], gw_coords[1], float(lat), float(lon)
        )

    def _save_longest_stations(self) -> None:
        """Persists Hall of Fame longest-distance stations to backup_dir only on record updates."""
        try:
            os.makedirs(self.backup_dir, exist_ok=True)
            with open(self._longest_stations_path(), "w", encoding="utf-8") as f:
                json.dump(self.get_longest_stations(), f, indent=2)
        except OSError as ex:
            logger.warning("Failed to persist longest_stations.json: %s", ex)

    def _record_longest_station(
        self,
        src: str,
        packet: Dict[str, Any],
        station: Dict[str, Any],
        dist_km: float,
        bearing: int,
        now_iso: str,
    ) -> bool:
        """Updates Hall of Fame if this packet sets a new distance record for the station."""
        is_server = bool(packet.get("is_server", False))
        local_call = (self.last_config or {}).get("callsign", "").strip().upper()
        if local_call and src.split("-")[0].upper() == local_call and dist_km < 0.05:
            return False

        key = f"{src}|{'IS' if is_server else 'RF'}"
        existing = self.longest_stations.get(key)
        if existing and dist_km <= float(existing.get("max_distance_km", -1.0)):
            return False

        entry = {
            "callsign": src,
            "max_distance_km": dist_km,
            "bearing_deg": bearing,
            "lat": packet.get("lat"),
            "lon": packet.get("lon"),
            "is_server": is_server,
            "path": packet.get("path") or station.get("path", ""),
            "comment": packet.get("comment") or station.get("comment", ""),
            "symbol_table": packet.get(
                "symbol_table", station.get("symbol_table", "/")
            ),
            "symbol_code": packet.get("symbol_code", station.get("symbol_code", "")),
            "recorded_at": now_iso,
        }
        self.longest_stations[key] = entry

        # Cap RF and IS buckets separately so APRS-IS traffic cannot evict RF DX records
        rf_items = sorted(
            (
                (k, v)
                for k, v in self.longest_stations.items()
                if not v.get("is_server")
            ),
            key=lambda kv: float(kv[1].get("max_distance_km", 0.0)),
            reverse=True,
        )[:MAX_LONGEST_STATIONS]
        is_items = sorted(
            ((k, v) for k, v in self.longest_stations.items() if v.get("is_server")),
            key=lambda kv: float(kv[1].get("max_distance_km", 0.0)),
            reverse=True,
        )[:MAX_LONGEST_STATIONS]

        new_map = dict(rf_items + is_items)
        kept = key in new_map
        self.longest_stations = new_map
        if kept:
            self._save_longest_stations()
        return kept

    def get_longest_stations(self, rf_only: bool = False) -> List[Dict[str, Any]]:
        items = [
            v
            for v in self.longest_stations.values()
            if not (rf_only and v.get("is_server"))
        ]
        items.sort(key=lambda s: float(s.get("max_distance_km", 0.0)), reverse=True)
        return items

    def _safekeep_eeprom(self, raw_512: bytes, parsed_config: Dict[str, Any]):
        """
        Safekeeps the raw 512-byte EEPROM binary and parsed JSON to disk:
        - `eeprom_initial.bin` and `eeprom_initial.json` are WRITE-ONCE (never overwritten),
          preserving the exact pristine state of the first download from the device.
        - `eeprom_latest.bin` and `eeprom_latest.json` are ONLY written when the 512 bytes
          actually change, preventing unnecessary SD/USB flash wear on routine #HELLO reads.
        """
        if len(raw_512) < 512:
            return
        try:
            os.makedirs(self.backup_dir, exist_ok=True)
            initial_bin = os.path.join(self.backup_dir, "eeprom_initial.bin")
            initial_json = os.path.join(self.backup_dir, "eeprom_initial.json")
            latest_bin = os.path.join(self.backup_dir, "eeprom_latest.bin")
            latest_json = os.path.join(self.backup_dir, "eeprom_latest.json")

            payload_512 = bytes(raw_512[:512])
            json_text = json.dumps(parsed_config, indent=2, sort_keys=True)

            if not os.path.exists(initial_bin):
                with open(initial_bin, "wb") as f:
                    f.write(payload_512)
                with open(initial_json, "w", encoding="utf-8") as f:
                    f.write(json_text)
                logger.info(
                    "Safekept first EEPROM download to %s (write-once golden backup)",
                    initial_bin,
                )

            existing_latest = None
            if os.path.isfile(latest_bin):
                try:
                    with open(latest_bin, "rb") as f:
                        existing_latest = f.read(512)
                except OSError:
                    existing_latest = None

            if existing_latest != payload_512:
                with open(latest_bin, "wb") as f:
                    f.write(payload_512)
                with open(latest_json, "w", encoding="utf-8") as f:
                    f.write(json_text)
                logger.info("Updated %s (EEPROM bytes changed)", latest_bin)
        except OSError as ex:
            logger.warning("Failed to safekeep EEPROM backup on disk: %s", ex)

    def get_initial_eeprom_bytes(self) -> Optional[bytes]:
        initial_bin = os.path.join(self.backup_dir, "eeprom_initial.bin")
        if os.path.isfile(initial_bin):
            try:
                with open(initial_bin, "rb") as f:
                    data = f.read(512)
                if len(data) == 512:
                    return data
            except OSError as ex:
                logger.warning("Failed to read initial EEPROM backup: %s", ex)
        return None

    async def _listen_loop(self):
        while self.connected and self.reader:
            try:
                chunk = await self.reader.read(4096)
                if not chunk:
                    if self.state != ConnectionState.REBOOTING:
                        logger.warning("Connection closed by remote NET51TNC")
                    break

                framed = self.framer.feed(chunk)

                # 1. Config Payload Handling
                if framed["config_ready"]:
                    payload = framed["config_ready"]
                    self.raw_template = payload[6:518]
                    try:
                        self.last_config = parse_eeprom(self.raw_template)
                        self._safekeep_eeprom(self.raw_template, self.last_config)
                        self.config_state = ConfigSyncState.SYNCED
                        self._config_event.set()
                        await self.broadcast(
                            {
                                "type": "config_update",
                                "config": self.last_config,
                                "status": self.latest_status,
                                "weather": self.latest_weather,
                                "config_state": self.config_state.value,
                            }
                        )
                    except ValueError as ex:
                        self.config_state = ConfigSyncState.ERROR
                        logger.error("Error parsing EEPROM: %s", ex)

                # 2. Complete Lines Handling (Split packets cleanly assembled)
                for line in framed["lines"]:
                    packet = parse_aprs_packet(line)
                    if packet and packet.get("is_packet"):
                        src = packet["source"]
                        now_dt = datetime.now(timezone.utc)
                        now_iso = now_dt.isoformat()
                        freshness, age_sec = compute_station_freshness(now_iso, now_dt)

                        existing = self.heard_stations.get(src)
                        prev_heard_iso = (
                            existing.get("last_heard") if existing else None
                        )
                        is_echo = False
                        if (
                            existing
                            and packet.get("tnc_tag") == "TNC->IS"
                            and prev_heard_iso
                        ):
                            _, prev_age = compute_station_freshness(
                                prev_heard_iso, now_dt
                            )
                            if prev_age < 3.0:
                                is_echo = True

                        station = existing or {
                            "callsign": src,
                            "first_heard": now_iso,
                            "packet_count": 0,
                            "lat": None,
                            "lon": None,
                            "comment": "",
                            "path": packet.get("path", ""),
                        }
                        station["last_heard"] = now_iso
                        if not is_echo:
                            station["packet_count"] += 1
                        station["last_raw"] = line
                        station["is_server"] = packet.get("is_server", False)
                        station["freshness"] = freshness.value
                        station["age_sec"] = round(age_sec, 1)

                        if packet.get("lat") is not None:
                            station["lat"] = packet["lat"]
                            station["lon"] = packet["lon"]
                            station["symbol_table"] = packet.get("symbol_table", "/")
                            station["symbol_code"] = packet.get("symbol_code", "")
                            track = station.setdefault("track", [])
                            if (
                                not track
                                or track[-1]["lat"] != packet["lat"]
                                or track[-1]["lon"] != packet["lon"]
                            ):
                                track.append(
                                    {
                                        "lat": packet["lat"],
                                        "lon": packet["lon"],
                                        "time": now_iso,
                                    }
                                )
                                if len(track) > 50:
                                    track.pop(0)
                        if packet.get("comment"):
                            station["comment"] = packet["comment"]
                        if packet.get("weather"):
                            station["weather"] = packet["weather"]
                            # Only update local gateway weather if packet matches configured local callsign
                            local_call = (
                                (self.last_config or {}).get("callsign", "").upper()
                            )
                            if local_call and src.split("-")[0] == local_call:
                                self.latest_weather = packet["weather"]

                        gw_coords = self._get_gateway_coords()
                        self._enrich_station_distance(station, gw_coords)
                        hof_updated = False
                        if (
                            gw_coords is not None
                            and packet.get("lat") is not None
                            and packet.get("lon") is not None
                        ):
                            dist_km = haversine_km(
                                gw_coords[0],
                                gw_coords[1],
                                float(packet["lat"]),
                                float(packet["lon"]),
                            )
                            brng = bearing_deg(
                                gw_coords[0],
                                gw_coords[1],
                                float(packet["lat"]),
                                float(packet["lon"]),
                            )
                            packet["distance_km"] = dist_km
                            packet["bearing_deg"] = brng
                            hof_updated = self._record_longest_station(
                                src, packet, station, dist_km, brng, now_iso
                            )

                        self._record_station(src, station)

                        await self.broadcast(
                            {
                                "type": "packet",
                                "packet": packet,
                                "station": station,
                            }
                        )
                        if hof_updated:
                            await self.broadcast(
                                {
                                    "type": "longest_stations_update",
                                    "longest_stations": self.get_longest_stations(),
                                }
                            )
                    else:
                        # Non-packet raw telemetry / status line from TNC hardware
                        wx_check = (
                            packet.get("weather")
                            if packet
                            else parse_weather_telemetry(line)
                        )
                        if wx_check:
                            self.latest_weather = wx_check
                            await self.broadcast(
                                {
                                    "type": "weather",
                                    "weather": wx_check,
                                }
                            )

                        self._parse_status_text(line)

                        await self.broadcast(
                            {
                                "type": "raw_line",
                                "line": line,
                                "status": self.latest_status,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            }
                        )

            except asyncio.CancelledError:
                break
            except OSError as e:
                logger.error("Socket error in TNC listener: %s", e)
                break

        if self.state != ConnectionState.REBOOTING:
            await self.transition_state(ConnectionState.DISCONNECTED, "listener_closed")

    async def send_command(self, cmd: bytes):
        if self.state == ConnectionState.REBOOTING:
            raise HTTPException(
                status_code=409,
                detail=f"TNC is rebooting ({self.reboot_seconds_remaining or 0}s remaining). Please wait.",
            )
        if not self.connected or not self.writer:
            raise HTTPException(status_code=400, detail="Not connected to NET51TNC")

        async with self._io_lock:
            self.writer.write(cmd)
            await asyncio.wait_for(self.writer.drain(), timeout=5.0)
            logger.info("Sent command: %s", cmd)

        if cmd in (CMD_RESET, CMD_INSTALL):
            wait_sec = 10 if cmd == CMD_INSTALL else 8
            reason = "factory_reset" if cmd == CMD_INSTALL else "reboot_command"
            self.trigger_reboot_lifecycle(reason, wait_seconds=wait_sec)

    async def read_config(self) -> Dict[str, Any]:
        if self.state == ConnectionState.REBOOTING:
            raise HTTPException(
                status_code=409,
                detail=f"TNC is rebooting ({self.reboot_seconds_remaining or 0}s remaining). Please wait.",
            )
        if not self.connected or not self.writer or not self.reader:
            raise HTTPException(status_code=400, detail="Not connected to NET51TNC")

        async with self._io_lock:
            self._config_event.clear()
            self.config_state = ConfigSyncState.SYNCING
            self.framer.set_state(StreamFramerState.READING_CONFIG)

            self.writer.write(CMD_READ)
            await asyncio.wait_for(self.writer.drain(), timeout=5.0)
            logger.info("Sent #HELLO command to read configuration...")

            try:
                await asyncio.wait_for(self._config_event.wait(), timeout=6.0)
            except asyncio.TimeoutError as exc:
                self.framer.set_state(StreamFramerState.STREAM)
                self.config_state = ConfigSyncState.ERROR
                raise HTTPException(
                    status_code=504,
                    detail="Timeout waiting for #HELLO config response from TNC",
                ) from exc

        if not self.last_config:
            self.config_state = ConfigSyncState.ERROR
            raise HTTPException(
                status_code=500, detail="Failed to parse configuration response"
            )

        self.config_state = ConfigSyncState.SYNCED
        return self.last_config

    async def write_config(
        self, new_config: Dict[str, Any], allow_uninitialized: bool = False
    ) -> Dict[str, Any]:
        if self.state == ConnectionState.REBOOTING:
            raise HTTPException(
                status_code=409,
                detail=f"TNC is already rebooting ({self.reboot_seconds_remaining or 0}s remaining).",
            )
        if not self.connected or not self.writer:
            raise HTTPException(status_code=400, detail="Not connected to NET51TNC")

        if self.raw_template is None and not allow_uninitialized:
            raise HTTPException(
                status_code=409,
                detail="EEPROM template has not been read from the TNC yet. Read configuration (#HELLO) before writing to preserve factory/undocumented bytes.",
            )

        try:
            eeprom_512 = build_eeprom(new_config, template=self.raw_template)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        payload = CMD_READ + eeprom_512
        if len(payload) != 518:
            raise HTTPException(
                status_code=500, detail=f"Invalid payload length {len(payload)}"
            )

        async with self._io_lock:
            self.config_state = ConfigSyncState.SAVING
            self.writer.write(payload)
            await asyncio.wait_for(self.writer.drain(), timeout=5.0)
            logger.info(
                "Wrote 518 bytes config payload to NET51TNC for %s",
                new_config.get("callsign"),
            )
            self.last_config = parse_eeprom(eeprom_512)
            self.raw_template = eeprom_512
            self._safekeep_eeprom(eeprom_512, self.last_config)

        self.trigger_reboot_lifecycle("config_saved", wait_seconds=8)

        return {
            "status": "success",
            "message": "Configuration written to EEPROM. TNC is restarting.",
            "state": self.state.value,
            "reboot_seconds": 8,
            "config": self.last_config,
        }

    async def broadcast(self, message: Dict[str, Any]):
        if not self.active_websockets:
            return
        snapshot = list(self.active_websockets)

        async def _send_one(ws: WebSocket) -> Optional[WebSocket]:
            try:
                await asyncio.wait_for(ws.send_json(message), timeout=2.0)
                return None
            except (WebSocketDisconnect, RuntimeError, OSError, asyncio.TimeoutError):
                return ws

        results = await asyncio.gather(*(_send_one(ws) for ws in snapshot))
        for dead_ws in results:
            if dead_ws is not None and dead_ws in self.active_websockets:
                self.active_websockets.remove(dead_ws)


tnc = TNCManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    reconnect_task = asyncio.create_task(tnc.auto_reconnect_loop())
    yield
    reconnect_task.cancel()
    await tnc.disconnect()


app = FastAPI(title="Web51TNC Controller", version="1.1.0", lifespan=lifespan)

cors_origins_env = os.environ.get("CORS_ORIGINS", "").strip()
if cors_origins_env:
    allowed_origins = [o.strip() for o in cors_origins_env.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "X-API-Token"],
    )


@app.get("/api/status")
async def get_status():
    return {
        # Backward-compatible keys
        "connected": tnc.connected,
        "connecting": tnc.connecting,
        "ip": tnc.ip,
        "port": tnc.port,
        "station_count": len(tnc.heard_stations),
        "has_config": tnc.last_config is not None,
        "status": tnc.latest_status,
        "weather": tnc.latest_weather,
        # Explicit State Machines for UX / DX / AX
        "state": tnc.state.value,
        "config_state": tnc.config_state.value,
        "reboot_seconds_remaining": tnc.reboot_seconds_remaining,
    }


@app.get("/api/state")
async def get_state_summary():
    """
    Concise, high-density state endpoint specifically tailored for autonomous agents (AX).
    Returns active state machines without heavy telemetry or station dumps.
    """
    now_dt = datetime.now(timezone.utc)
    active_cnt = 0
    rf_cnt = 0
    for s in tnc.heard_stations.values():
        f_state, _ = compute_station_freshness(s.get("last_heard"), now_dt)
        if f_state == StationFreshness.ACTIVE:
            active_cnt += 1
        if not s.get("is_server"):
            rf_cnt += 1

    return {
        "connection_state": tnc.state.value,
        "config_state": tnc.config_state.value,
        "reboot_seconds_remaining": tnc.reboot_seconds_remaining,
        "is_ready": tnc.state == ConnectionState.CONNECTED
        and tnc.config_state != ConfigSyncState.SAVING,
        "ip": tnc.ip,
        "port": tnc.port,
        "total_stations": len(tnc.heard_stations),
        "active_stations": active_cnt,
        "rf_stations": rf_cnt,
        "has_weather": tnc.latest_weather is not None,
    }


@app.get("/api/weather")
async def get_weather():
    return tnc.latest_weather or {}


@app.post("/api/connect", dependencies=[Depends(verify_api_token)])
async def connect_tnc(req: ConnectRequest):
    await tnc.connect(req.ip, req.port)
    return {
        "status": "connected",
        "state": tnc.state.value,
        "ip": req.ip,
        "port": req.port,
    }


@app.post("/api/disconnect", dependencies=[Depends(verify_api_token)])
async def disconnect_tnc():
    await tnc.disconnect()
    return {"status": "disconnected", "state": tnc.state.value}


@app.get("/api/config", dependencies=[Depends(verify_api_token)])
async def get_config():
    if tnc.state == ConnectionState.REBOOTING:
        raise HTTPException(
            status_code=409,
            detail=f"TNC is rebooting ({tnc.reboot_seconds_remaining or 0}s remaining). Please wait.",
        )
    if not tnc.connected:
        if tnc.last_config:
            cached = dict(tnc.last_config)
            cached["_cached"] = True
            return cached
        raise HTTPException(status_code=400, detail="Not connected to NET51TNC")
    config = await tnc.read_config()
    return config


@app.post("/api/config", dependencies=[Depends(verify_api_token)])
async def save_config(config: TNCConfigModel, force: bool = False):
    merged: Dict[str, Any] = {}
    if tnc.last_config:
        merged.update(tnc.last_config)
    merged.update(config.model_dump(exclude_unset=True))
    return await tnc.write_config(merged, allow_uninitialized=force)


@app.get("/api/passcode/{callsign}")
async def get_passcode(callsign: str):
    """Calculates the standard APRS-IS passcode for the given callsign."""
    clean = callsign.strip().upper().split("-")[0]
    if not CALLSIGN_RE.match(clean):
        raise HTTPException(
            status_code=422, detail=f"Invalid amateur callsign: {callsign!r}"
        )
    return {"callsign": clean, "passcode": str(compute_aprs_passcode(clean))}


def _query_wifi_status_sync() -> Dict[str, Any]:
    mode = "disconnected"
    ssid = ""
    ip_addr = ""
    try:
        res = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev", "status"],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        for line in res.stdout.splitlines():
            parts = line.split(":", 3)
            if len(parts) >= 4 and parts[0].startswith(("wlan", "wlp")):
                state, conn = parts[2], parts[3]
                if state == "connected":
                    if conn == "Web51TNC-AP":
                        mode = "ap"
                        ssid = "Web51TNC-Setup"
                    else:
                        mode = "client"
                        ssid = conn
                break
        ip_res = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", "wlan0"],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        for line in ip_res.stdout.splitlines():
            tokens = line.split()
            for idx, tok in enumerate(tokens):
                if tok == "inet" and idx + 1 < len(tokens):
                    ip_addr = tokens[idx + 1].split("/")[0]
                    break
        if ip_addr == "10.42.0.1":
            mode = "ap"
            if not ssid:
                ssid = "Web51TNC-Setup"
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Wi-Fi status check warning: %s", exc)

    cur_call = str((tnc.last_config or {}).get("callsign", "")).strip().upper()
    unconfigured_call = cur_call in ("", "NOCALL", "N0CALL", "BG5FCH", "CALL")
    return {
        "mode": mode,
        "ssid": ssid,
        "ip": ip_addr,
        "ap_ssid": "Web51TNC-Setup",
        "ap_ip": "10.42.0.1",
        "needs_setup": (mode == "ap") or unconfigured_call,
    }


def _scan_wifi_sync() -> List[Dict[str, Any]]:
    networks: Dict[str, Dict[str, Any]] = {}
    try:
        res = subprocess.run(
            [
                "sudo",
                "-n",
                "nmcli",
                "-t",
                "-e",
                "no",
                "-f",
                "ACTIVE,SSID,SIGNAL,SECURITY",
                "dev",
                "wifi",
                "list",
                "--rescan",
                "auto",
            ],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
        if res.returncode != 0:
            res = subprocess.run(
                [
                    "nmcli",
                    "-t",
                    "-e",
                    "no",
                    "-f",
                    "ACTIVE,SSID,SIGNAL,SECURITY",
                    "dev",
                    "wifi",
                    "list",
                ],
                capture_output=True,
                text=True,
                timeout=8.0,
                check=False,
            )
        for line in res.stdout.splitlines():
            parts = line.split(":", 3)
            if len(parts) < 4:
                continue
            active_str, net_ssid, sig_str, sec = (
                parts[0],
                parts[1].strip(),
                parts[2].strip(),
                parts[3].strip(),
            )
            if not net_ssid or net_ssid == "Web51TNC-Setup":
                continue
            try:
                sig = int(sig_str)
            except ValueError:
                sig = 0
            is_act = active_str == "yes"
            prev = networks.get(net_ssid)
            if prev is None or sig > prev["signal"] or is_act:
                networks[net_ssid] = {
                    "ssid": net_ssid,
                    "signal": sig,
                    "security": sec or "Open",
                    "active": is_act or (prev["active"] if prev else False),
                }
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Wi-Fi scan failed: %s", exc)
    return sorted(networks.values(), key=lambda x: (not x["active"], -x["signal"]))


def _connect_wifi_sync(ssid: str, password: str) -> Dict[str, Any]:
    clean_ssid = ssid.strip()
    if not clean_ssid:
        raise ValueError("SSID cannot be empty")
    # Stop Hotspot AP if active so wlan0 can associate as client
    if os.path.exists("/usr/local/sbin/web51tnc-ap-ctl"):
        subprocess.run(
            ["sudo", "-n", "/usr/local/sbin/web51tnc-ap-ctl", "stop"],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=False,
        )
    cmd = [
        "sudo",
        "-n",
        "nmcli",
        "--wait",
        "20",
        "dev",
        "wifi",
        "connect",
        clean_ssid,
        "ifname",
        "wlan0",
    ]
    if password:
        cmd.extend(["password", password])
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=25.0, check=False)
    if res.returncode != 0:
        # Fallback: create/update profile directly and bring it up
        subprocess.run(
            ["sudo", "-n", "nmcli", "connection", "delete", clean_ssid],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
        add_cmd = [
            "sudo",
            "-n",
            "nmcli",
            "connection",
            "add",
            "type",
            "wifi",
            "ifname",
            "wlan0",
            "con-name",
            clean_ssid,
            "ssid",
            clean_ssid,
            "connection.autoconnect",
            "yes",
            "connection.autoconnect-priority",
            "150",
            "802-11-wireless.powersave",
            "2",
        ]
        if password:
            add_cmd.extend(["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password])
        subprocess.run(
            add_cmd, capture_output=True, text=True, timeout=8.0, check=False
        )
        up_res = subprocess.run(
            ["sudo", "-n", "nmcli", "--wait", "20", "connection", "up", clean_ssid],
            capture_output=True,
            text=True,
            timeout=25.0,
            check=False,
        )
        if up_res.returncode != 0:
            if os.path.exists("/usr/local/sbin/web51tnc-ap-ctl"):
                subprocess.run(
                    ["sudo", "-n", "/usr/local/sbin/web51tnc-ap-ctl", "start"],
                    capture_output=True,
                    text=True,
                    timeout=10.0,
                    check=False,
                )
            err_msg = (
                up_res.stderr or up_res.stdout or res.stderr or "Connection failed"
            ).strip()
            raise RuntimeError(f"Failed to connect to '{clean_ssid}': {err_msg}")
    else:
        subprocess.run(
            [
                "sudo",
                "-n",
                "nmcli",
                "connection",
                "modify",
                clean_ssid,
                "connection.autoconnect",
                "yes",
                "connection.autoconnect-priority",
                "150",
                "802-11-wireless.powersave",
                "2",
            ],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    return _query_wifi_status_sync()


@app.get("/api/wifi/status")
async def get_wifi_status():
    return await asyncio.to_thread(_query_wifi_status_sync)


@app.get("/api/wifi/scan", dependencies=[Depends(verify_api_token)])
async def scan_wifi_networks():
    nets = await asyncio.to_thread(_scan_wifi_sync)
    return {"networks": nets}


@app.post("/api/wifi/connect", dependencies=[Depends(verify_api_token)])
async def connect_wifi_network(req: WifiConnectRequest):
    try:
        status = await asyncio.to_thread(_connect_wifi_sync, req.ssid, req.password)
        return {"status": "connected", "wifi": status}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/wifi/ap", dependencies=[Depends(verify_api_token)])
async def control_wifi_ap(req: WifiApRequest):
    def _run_ap():
        if os.path.exists("/usr/local/sbin/web51tnc-ap-ctl"):
            subprocess.run(
                ["sudo", "-n", "/usr/local/sbin/web51tnc-ap-ctl", req.action],
                capture_output=True,
                text=True,
                timeout=15.0,
                check=False,
            )
        return _query_wifi_status_sync()

    status = await asyncio.to_thread(_run_ap)
    return {"status": req.action, "wifi": status}


@app.get("/api/eeprom/raw", dependencies=[Depends(verify_api_token)])
async def download_raw_eeprom():
    """Downloads the current raw 512-byte EEPROM binary image for backup/restore."""
    if tnc.raw_template is None or len(tnc.raw_template) < 512:
        raise HTTPException(
            status_code=404,
            detail="No EEPROM image cached yet. Read from TNC (#HELLO) first.",
        )
    call = (tnc.last_config or {}).get("callsign", "NET51TNC")
    return Response(
        content=bytes(tnc.raw_template[:512]),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{call}_eeprom_512b.bin"'
        },
    )


@app.get("/api/eeprom/initial", dependencies=[Depends(verify_api_token)])
async def get_initial_eeprom(raw: bool = False):
    """
    Returns the safekept first EEPROM download from the device (write-once golden backup).
    Pass ?raw=true to download the raw 512-byte binary, or omit for parsed JSON.
    """
    initial_bytes = tnc.get_initial_eeprom_bytes()
    if initial_bytes is None:
        raise HTTPException(
            status_code=404, detail="No initial EEPROM backup has been recorded yet."
        )
    parsed = parse_eeprom(initial_bytes)
    if raw:
        call = parsed.get("callsign", "NET51TNC")
        return Response(
            content=initial_bytes,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{call}_eeprom_initial_512b.bin"'
            },
        )
    return parsed


@app.post("/api/command/{cmd_name}", dependencies=[Depends(verify_api_token)])
async def run_command(cmd_name: str):
    cmd_map = {
        "reset": CMD_RESET,
        "install": CMD_INSTALL,
        "gate_rf_test": CMD_GATE_RF_TEST,
        "gate_is_test": CMD_GATE_IS_TEST,
        "wx_rf_test": CMD_WX_RF_TEST,
        "wx_is_test": CMD_WX_IS_TEST,
        "hello": CMD_READ,
    }
    if cmd_name not in cmd_map:
        raise HTTPException(status_code=404, detail=f"Unknown command: {cmd_name}")

    if cmd_name == "hello":
        cfg = await tnc.read_config()
        return {
            "status": "sent",
            "command": cmd_name,
            "state": tnc.state.value,
            "config": cfg,
        }

    await tnc.send_command(cmd_map[cmd_name])
    return {"status": "sent", "command": cmd_name, "state": tnc.state.value}


@app.get("/api/stations")
async def get_stations(freshness: Optional[str] = None, rf_only: bool = False):
    now_dt = datetime.now(timezone.utc)
    if tnc._prune_expired_stations(now_dt):
        tnc._save_heard_stations()
    gw_coords = tnc._get_gateway_coords()
    result = []
    for s in tnc.heard_stations.values():
        f_state, age_sec = compute_station_freshness(s.get("last_heard"), now_dt)
        s["freshness"] = f_state.value
        s["age_sec"] = round(age_sec, 1)
        tnc._enrich_station_distance(s, gw_coords)

        if rf_only and s.get("is_server"):
            continue
        if freshness and s["freshness"] != freshness:
            continue
        result.append(s)
    return result


@app.delete("/api/stations", dependencies=[Depends(verify_api_token)])
async def clear_stations():
    tnc.heard_stations.clear()
    tnc._save_heard_stations()
    await tnc.broadcast({"type": "stations_cleared"})
    return {"status": "cleared"}


@app.get("/api/stations/longest")
async def get_longest_stations(rf_only: bool = False):
    return tnc.get_longest_stations(rf_only=rf_only)


@app.delete("/api/stations/longest", dependencies=[Depends(verify_api_token)])
async def clear_longest_stations():
    tnc.longest_stations.clear()
    tnc._save_longest_stations()
    await tnc.broadcast({"type": "longest_stations_cleared"})
    return {"status": "cleared"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    tnc.active_websockets.append(websocket)
    try:
        now_dt = datetime.now(timezone.utc)
        if tnc._prune_expired_stations(now_dt):
            tnc._save_heard_stations()
        gw_coords = tnc._get_gateway_coords()
        stations = []
        for s in tnc.heard_stations.values():
            f_state, age_sec = compute_station_freshness(s.get("last_heard"), now_dt)
            s["freshness"] = f_state.value
            s["age_sec"] = round(age_sec, 1)
            tnc._enrich_station_distance(s, gw_coords)
            stations.append(s)

        await websocket.send_json(
            {
                "type": "init",
                "state": tnc.state.value,
                "connected": tnc.connected,
                "connecting": tnc.connecting,
                "config_state": tnc.config_state.value,
                "reboot_seconds": tnc.reboot_seconds_remaining,
                "ip": tnc.ip,
                "port": tnc.port,
                "stations": stations,
                "longest_stations": tnc.get_longest_stations(),
                "config": tnc.last_config,
                "status": tnc.latest_status,
                "weather": tnc.latest_weather,
            }
        )
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
    finally:
        if websocket in tnc.active_websockets:
            tnc.active_websockets.remove(websocket)


# Mount static files if present
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    index_file = os.path.join(static_dir, "index.html")
    if os.path.exists(index_file):
        return FileResponse(
            index_file, headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
        )
    return HTMLResponse("<h1>Web51TNC UI is building...</h1>")


def main():
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(
        description="Web51TNC APRS Controller & Web Server"
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="Web bind host (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="Web bind port (default: 8000)",
    )
    parser.add_argument(
        "--tnc-ip",
        default=os.environ.get("TNC_IP", DEFAULT_IP),
        help="NET-51TNC IP address (default: 192.168.1.120)",
    )
    parser.add_argument(
        "--tnc-port",
        type=int,
        default=int(os.environ.get("TNC_PORT", str(DEFAULT_PORT))),
        help="NET-51TNC port (default: 60000)",
    )
    parser.add_argument(
        "--no-auto-connect",
        action="store_true",
        help="Disable background auto-reconnect",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable uvicorn hot-reload (development only)",
    )

    args = parser.parse_args()

    # Export CLI args to environment so uvicorn reload workers inherit them
    os.environ["TNC_IP"] = args.tnc_ip
    os.environ["TNC_PORT"] = str(args.tnc_port)
    if args.no_auto_connect:
        os.environ["AUTO_CONNECT"] = "0"
        tnc.auto_reconnect = False

    tnc.ip = args.tnc_ip
    tnc.port = args.tnc_port

    logger.info("Starting Web51TNC on http://%s:%d", args.host, args.port)
    logger.info(
        "Target NET-51TNC: %s:%d (auto-reconnect: %s)",
        tnc.ip,
        tnc.port,
        tnc.auto_reconnect,
    )

    if args.reload:
        uvicorn.run("server:app", host=args.host, port=args.port, reload=True)
    else:
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
