/**
 * Web51TNC — Client Application Logic & State Machines
 * Unified UX, DX, and AX Reactive Controller
 */

let map = null;
let homeMarker = null;
let homeCoords = null;
let markers = {};
let polylines = {};
let heardStations = {};
let ws;

const MAX_CONSOLE_LINES = 500;
const MAX_STATION_AGE_SEC = 7200; // 2 hours max live map retention

// State Machine Model
const appState = {
  wsConnected: false,
  tncState: "disconnected", // "disconnected" | "connecting" | "connected" | "rebooting"
  rebootSeconds: null,
  configState: "unknown",   // "unknown" | "synced" | "dirty" | "saving" | "syncing"
  lastLoadedConfig: null,
  homeCallsign: null,
  stationTimeZone: (typeof Intl !== "undefined" && Intl.DateTimeFormat().resolvedOptions().timeZone) || "Europe/Helsinki",
  pickingCoords: false,
  ip: "192.168.1.120",
  port: 60000,
};

function resolveStationTimeZone(lat, lon, callsign) {
  const call = (callsign || "").trim().toUpperCase();
  // 1. Check coordinates first if valid
  if (typeof lat === "number" && !Number.isNaN(lat) && typeof lon === "number" && !Number.isNaN(lon)) {
    // Finland (including Lapland & Åland)
    if (lat >= 59.4 && lat <= 70.3 && lon >= 19.0 && lon <= 31.6) {
      if (call.startsWith("OH") || call.startsWith("OF") || call.startsWith("OG") || call.startsWith("OI") || lon >= 20.5) {
        return "Europe/Helsinki";
      }
    }
    // Baltic States (Estonia, Latvia, Lithuania)
    if (lat >= 53.8 && lat < 59.8 && lon >= 20.9 && lon <= 28.3) {
      if (lat >= 57.5) return "Europe/Tallinn";
      if (lat >= 55.6) return "Europe/Riga";
      return "Europe/Vilnius";
    }
    // Sweden & Norway
    if (lat >= 55.0 && lat <= 71.5 && lon >= 4.4 && lon < 24.2) {
      return call.startsWith("LA") || call.startsWith("LB") || lon < 11.5 ? "Europe/Oslo" : "Europe/Stockholm";
    }
    // UK & Ireland
    if (lat >= 49.8 && lat <= 61.0 && lon >= -10.8 && lon <= 1.8) {
      return lon < -5.4 && lat < 55.5 ? "Europe/Dublin" : "Europe/London";
    }
    // Portugal
    if (lat >= 36.8 && lat <= 42.2 && lon >= -9.6 && lon <= -6.1) {
      return "Europe/Lisbon";
    }
    // Western & Central Europe
    if (lat >= 35.8 && lat <= 55.2 && lon >= -5.5 && lon <= 24.0) {
      return "Europe/Berlin";
    }
    // Eastern Europe (Greece, Romania, Bulgaria, Ukraine)
    if (lat >= 34.8 && lat <= 52.5 && lon > 24.0 && lon <= 40.0) {
      return "Europe/Helsinki";
    }
    // China
    if (lat >= 18.0 && lat <= 53.6 && lon >= 73.5 && lon <= 135.0) {
      return "Asia/Shanghai";
    }
    // Japan
    if (lat >= 24.0 && lat <= 45.6 && lon >= 122.9 && lon <= 145.9) {
      return "Asia/Tokyo";
    }
  }

  // 2. Fallback to ITU callsign prefix mapping
  const prefixMap = [
    [/^(OH|OF|OG|OI)/, "Europe/Helsinki"],
    [/^(SM|SA|SB|SC|SD|SE|SF|SG|SH|SI|SJ|SK|SL|7S|8S)/, "Europe/Stockholm"],
    [/^(LA|LB|LC|LD|LE|LF|LG|LH|LI|LJ|LK|LL|LM|LN)/, "Europe/Oslo"],
    [/^(OZ|OU|OV|OW|5P|5Q)/, "Europe/Copenhagen"],
    [/^ES/, "Europe/Tallinn"],
    [/^YL/, "Europe/Riga"],
    [/^LY/, "Europe/Vilnius"],
    [/^TF/, "Atlantic/Reykjavik"],
    [/^(G|M|2E|2M|2W|2I|2D|2U|2J)/, "Europe/London"],
    [/^(EI|EJ)/, "Europe/Dublin"],
    [/^(DL|DA|DB|DC|DD|DE|DF|DG|DH|DJ|DK|DM|DN|DO|DP|DQ|DR)/, "Europe/Berlin"],
    [/^(PA|PB|PC|PD|PE|PF|PG|PH|PI)/, "Europe/Amsterdam"],
    [/^(ON|OO|OP|OQ|OR|OS|OT)/, "Europe/Brussels"],
    [/^(F|TM|TK)/, "Europe/Paris"],
    [/^(EA|EB|EC|ED|EE|EF|EG|EH)/, "Europe/Madrid"],
    [/^CT/, "Europe/Lisbon"],
    [/^(HB|HE)/, "Europe/Zurich"],
    [/^OE/, "Europe/Vienna"],
    [/^(I|IK|IZ|IW|IU)/, "Europe/Rome"],
    [/^(SP|SQ|SN|SO|HF|3Z)/, "Europe/Warsaw"],
    [/^(OK|OL)/, "Europe/Prague"],
    [/^OM/, "Europe/Bratislava"],
    [/^(HA|HG)/, "Europe/Budapest"],
    [/^(YO|YP|YQ|YR)/, "Europe/Bucharest"],
    [/^LZ/, "Europe/Sofia"],
    [/^(SV|SW|SX|SY|SZ|J4)/, "Europe/Athens"],
    [/^(UR|US|UT|UU|UV|UW|UX|UY|UZ|EM|EN|EO)/, "Europe/Kyiv"],
    [/^(B[A-Z]|VR|XX)/, "Asia/Shanghai"],
    [/^(J[A-S]|7[J-N]|8[J-N])/, "Asia/Tokyo"],
  ];
  for (const [re, tz] of prefixMap) {
    if (re.test(call)) return tz;
  }

  return (typeof Intl !== "undefined" && Intl.DateTimeFormat().resolvedOptions().timeZone) || "Europe/Helsinki";
}

function formatLocalTime(isoOrDate, includeRelative = false) {
  if (!isoOrDate) return "";
  const dt = isoOrDate instanceof Date ? isoOrDate : new Date(isoOrDate);
  if (Number.isNaN(dt.getTime())) return String(isoOrDate);
  const tz = appState.stationTimeZone || "Europe/Helsinki";
  let timeStr;
  try {
    timeStr = new Intl.DateTimeFormat("sv-SE", {
      timeZone: tz,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(dt);
  } catch (_) {
    timeStr = dt.toTimeString().slice(0, 8);
  }
  if (!includeRelative) return timeStr;
  const ageSec = Math.max(0, Math.round((Date.now() - dt.getTime()) / 1000));
  const rel = ageSec < 60
    ? `${ageSec}s ago`
    : ageSec < 3600
    ? `${Math.floor(ageSec / 60)}m ago`
    : `${Math.floor(ageSec / 3600)}h ${Math.floor((ageSec % 3600) / 60)}m ago`;
  return `${timeStr} (${rel})`;
}

function updateHeaderClock() {
  const el = document.getElementById("header-tz");
  if (!el) return;
  const tz = appState.stationTimeZone || "Europe/Helsinki";
  const now = new Date();
  const timeStr = formatLocalTime(now, false);
  const city = tz.includes("/") ? tz.split("/").pop().replace(/_/g, " ") : tz;
  el.textContent = `🕒 ${timeStr} (${city})`;
  el.title = `Station Local Timezone: ${tz} (24h format)`;
}

// Visual APRS Symbol Catalog (Primary '/', Alternate '\', and Overlay variants)
const APRS_SYMBOL_CATALOG = [
  { table: "/", code: "r", icon: "🗼", name: "Repeater / Tower", short: "Repeater" },
  { table: "/", code: "&", icon: "📡", name: "HF / iGate Gateway", short: "iGate" },
  { table: "I", code: "&", icon: "🌐", name: "TX iGate (Overlay I)", short: "TX iGate" },
  { table: "R", code: "&", icon: "📻", name: "RX-Only iGate (Overlay R)", short: "RX iGate" },
  { table: "/", code: "#", icon: "⭐", name: "Digipeater (Star)", short: "Digipeater" },
  { table: "/", code: "-", icon: "🏠", name: "House / Home QTH", short: "Home QTH" },
  { table: "/", code: "_", icon: "🌦️", name: "Weather Station", short: "Weather" },
  { table: "/", code: "*", icon: "🛷", name: "Snowmobile", short: "Snowmobile" },
  { table: "/", code: ">", icon: "🚗", name: "Car / Mobile", short: "Car" },
  { table: "/", code: "k", icon: "🛻", name: "Truck / 4x4", short: "4x4 / Truck" },
  { table: "/", code: "<", icon: "🏍️", name: "Motorcycle", short: "Moto" },
  { table: "/", code: "b", icon: "🚲", name: "Bicycle", short: "Bike" },
  { table: "/", code: "[", icon: "🏃", name: "Hiker / Portable", short: "Hiker" },
  { table: "/", code: ";", icon: "⛺", name: "Campground / Field", short: "Camp" },
  { table: "/", code: "Y", icon: "⛵", name: "Sailboat / Yacht", short: "Sailboat" },
  { table: "/", code: "s", icon: "🚤", name: "Power Boat", short: "Boat" },
  { table: "/", code: "^", icon: "✈️", name: "Aircraft", short: "Aircraft" },
  { table: "/", code: "O", icon: "🎈", name: "Balloon / HAB", short: "Balloon" },
  { table: "/", code: "a", icon: "🚑", name: "Ambulance / Rescue", short: "Rescue" },
  { table: "/", code: "f", icon: "🚒", name: "Fire Truck", short: "Fire" },
];

const APRS_FALLBACK_CODE_ICONS = {
  "r": { icon: "🗼", name: "Repeater / Tower" },
  "&": { icon: "📡", name: "Gateway / iGate" },
  "#": { icon: "⭐", name: "Digipeater" },
  "-": { icon: "🏠", name: "Home QTH" },
  "_": { icon: "🌦️", name: "Weather Station" },
  "*": { icon: "🛷", name: "Snowmobile" },
  ">": { icon: "🚗", name: "Car / Mobile" },
  "k": { icon: "🛻", name: "Truck / 4x4" },
  "u": { icon: "🚚", name: "18-Wheeler Truck" },
  "v": { icon: "🚐", name: "Van" },
  "R": { icon: "🚐", name: "RV / Camper" },
  "<": { icon: "🏍️", name: "Motorcycle" },
  "b": { icon: "🚲", name: "Bicycle" },
  "[": { icon: "🏃", name: "Hiker / Portable" },
  ";": { icon: "⛺", name: "Campground / Portable" },
  "Y": { icon: "⛵", name: "Sailboat" },
  "s": { icon: "🚤", name: "Power Boat" },
  "C": { icon: "🛶", name: "Canoe / Kayak" },
  "^": { icon: "✈️", name: "Aircraft" },
  "X": { icon: "🚁", name: "Helicopter" },
  "O": { icon: "🎈", name: "Balloon" },
  "a": { icon: "🚑", name: "Ambulance" },
  "f": { icon: "🚒", name: "Fire Truck" },
  "!": { icon: "🚓", name: "Police / Emergency" },
  "h": { icon: "🏥", name: "Hospital" },
  "y": { icon: "📡", name: "Yagi Beam QTH" },
  "$": { icon: "📞", name: "Phone / Link" },
};

function getAprsSymbolMeta(table, code) {
  const tbl = (table || "/").charAt(0) || "/";
  const cd = (code || "r").charAt(0) || "r";
  const exact = APRS_SYMBOL_CATALOG.find(item => item.table === tbl && item.code === cd);
  if (exact) {
    return {
      icon: exact.icon,
      name: exact.name,
      short: exact.short,
      combo: `${tbl}${cd}`,
    };
  }
  const byCode = APRS_FALLBACK_CODE_ICONS[cd];
  if (byCode) {
    const overlayNote = (tbl !== "/" && tbl !== "\\") ? ` (Overlay ${tbl})` : "";
    return {
      icon: byCode.icon,
      name: `${byCode.name}${overlayNote}`,
      short: byCode.name,
      combo: `${tbl}${cd}`,
    };
  }
  return {
    icon: "📻",
    name: `APRS Symbol ${tbl}${cd}`,
    short: `${tbl}${cd}`,
    combo: `${tbl}${cd}`,
  };
}

// Application Bootstrap
document.addEventListener("DOMContentLoaded", () => {
  initMap();
  initTabs();
  renderGatewayIconPicker();
  initControls();
  initConfigDirtyTracking();
  initQuickSetupWizard();
  initWebSocket();
  updateHeaderClock();
  setInterval(updateHeaderClock, 1000);
  setInterval(refreshStationFreshness, 30000);
});

function refreshStationFreshness() {
  const nowMs = Date.now();
  for (const [call, s] of Object.entries(heardStations)) {
    if (!s.last_heard) continue;
    const ageSec = Math.max(0, (nowMs - new Date(s.last_heard).getTime()) / 1000);
    s.age_sec = Math.round(ageSec * 10) / 10;
    if (ageSec > MAX_STATION_AGE_SEC) {
      if (map) {
        if (markers[call]) map.removeLayer(markers[call]);
        if (polylines[call]) map.removeLayer(polylines[call]);
      }
      delete markers[call];
      delete polylines[call];
      delete heardStations[call];
      continue;
    }
    s.freshness = ageSec < 900 ? "active" : ageSec < 3600 ? "recent" : "stale";
  }
  renderStationsTable();
  updateMapStats();
}

// Toast Notifications System (Non-blocking AX & UX)
function showToast(message, type = "info", duration = 4000) {
  const container = document.getElementById("toast-container");
  if (!container) return;

  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  const icon = type === "success" ? "✓" : type === "error" ? "✕" : type === "warning" ? "⚡" : "ℹ";
  toast.innerHTML = `<span style="font-weight:bold; font-size:1.1rem;">${icon}</span><span>${escapeHtml(message)}</span>`;
  container.appendChild(toast);

  setTimeout(() => {
    toast.classList.add("toast-fadeout");
    setTimeout(() => toast.remove(), 250);
  }, duration);
}

// Accessible Non-blocking Confirmation Modal
function showConfirm(message, title = "Confirm Action") {
  return new Promise((resolve) => {
    const modal = document.getElementById("confirm-modal");
    const titleEl = document.getElementById("confirm-modal-title");
    const msgEl = document.getElementById("confirm-modal-message");
    const btnOk = document.getElementById("confirm-modal-ok");
    const btnCancel = document.getElementById("confirm-modal-cancel");

    if (!modal) {
      resolve(window.confirm(message));
      return;
    }

    titleEl.textContent = title;
    msgEl.textContent = message;
    modal.style.display = "flex";

    const onKey = (e) => {
      if (e.key === "Escape") {
        cleanup();
        resolve(false);
      }
    };
    window.addEventListener("keydown", onKey);

    const cleanup = () => {
      modal.style.display = "none";
      btnOk.onclick = null;
      btnCancel.onclick = null;
      window.removeEventListener("keydown", onKey);
    };

    btnOk.onclick = () => {
      cleanup();
      resolve(true);
    };

    btnCancel.onclick = () => {
      cleanup();
      resolve(false);
    };
  });
}

// Initialize Leaflet Map (guarded against offline/missing library)
function initMap() {
  if (typeof L === "undefined") {
    console.warn("[Map] Leaflet library unavailable; map view disabled.");
    return;
  }
  try {
    map = L.map("map").setView([60.1699, 24.9384], 10); // Default Helsinki, Finland

    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '© <a href="https://openstreetmap.org">OpenStreetMap</a> contributors'
    }).addTo(map);

    map.on("click", (e) => {
      if (!appState.pickingCoords) return;
      const lat = e.latlng.lat;
      const lon = e.latlng.lng;
      const aprsLat = decimalToAprs(lat, false);
      const aprsLon = decimalToAprs(lon, true);
      if (!aprsLat || !aprsLon) {
        showToast("Invalid map coordinates selected", "error");
        return;
      }

      setVal("cfg-gw-lat", aprsLat);
      setVal("cfg-gw-lon", aprsLon);
      setVal("calc-dec-lat", lat.toFixed(5));
      setVal("calc-dec-lon", lon.toFixed(5));
      setVal("qs-lat", lat.toFixed(5));
      setVal("qs-lon", lon.toFixed(5));
      checkConfigDirty();
      updateHomeStationMarker(gatherConfigForm(), false);
      const fromWizard = Boolean(appState.pickingFromWizard);
      stopMapCoordPicker();

      if (fromWizard) {
        const qsModal = document.getElementById("quick-setup-modal");
        if (qsModal) qsModal.style.display = "flex";
      } else {
        const cfgTabBtn = document.querySelector('.tab-btn[data-tab="tab-config"]');
        if (cfgTabBtn) cfgTabBtn.click();
      }
      showToast(`📍 Picked Gateway location: ${aprsLat} / ${aprsLon} (${lat.toFixed(5)}°, ${lon.toFixed(5)}°)`, "success", 5000);
    });
  } catch (err) {
    console.error("[Map] Failed to initialize Leaflet:", err);
  }
}

function startMapCoordPicker() {
  if (!map) {
    showToast("Interactive map is not available", "error");
    return;
  }
  appState.pickingCoords = true;
  const banner = document.getElementById("map-pick-banner");
  const mapEl = document.getElementById("map");
  if (banner) banner.style.display = "flex";
  if (mapEl) mapEl.classList.add("picking-coords");

  const mapTabBtn = document.querySelector('.tab-btn[data-tab="tab-map"]');
  if (mapTabBtn) mapTabBtn.click();

  setTimeout(() => {
    map.invalidateSize();
    const curLat = aprsToDecimal(getVal("cfg-gw-lat"), false);
    const curLon = aprsToDecimal(getVal("cfg-gw-lon"), true);
    if (curLat !== null && curLon !== null) {
      map.setView([parseFloat(curLat), parseFloat(curLon)], Math.max(map.getZoom(), 14));
    } else if (homeCoords) {
      map.setView(homeCoords, Math.max(map.getZoom(), 14));
    }
  }, 150);
}

function stopMapCoordPicker() {
  appState.pickingCoords = false;
  appState.pickingFromWizard = false;
  const banner = document.getElementById("map-pick-banner");
  const mapEl = document.getElementById("map");
  if (banner) banner.style.display = "none";
  if (mapEl) mapEl.classList.remove("picking-coords");
}

// Navigation Tabs
function initTabs() {
  const buttons = document.querySelectorAll(".nav-tabs .tab-btn");
  buttons.forEach(btn => {
    btn.addEventListener("click", () => {
      buttons.forEach(b => b.classList.remove("active"));
      document.querySelectorAll(".tab-pane").forEach(p => p.classList.remove("active"));

      btn.classList.add("active");
      const target = document.getElementById(btn.dataset.tab);
      if (target) {
        target.classList.add("active");
      }

      if (btn.dataset.tab === "tab-map" && map) {
        setTimeout(() => map.invalidateSize(), 200);
      }
    });
  });
}

// WebSocket Connection & Reactive State Machine
function initWebSocket() {
  const loc = window.location;
  const wsProtocol = loc.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${wsProtocol}//${loc.host}/ws`;

  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    console.log("[WebSocket] Link established with Web51TNC backend");
    appState.wsConnected = true;
    updateWsIndicator(true);
  };

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      handleWsMessage(data);
    } catch (e) {
      console.error("[WebSocket] Error parsing message:", e);
    }
  };

  ws.onclose = () => {
    console.warn("[WebSocket] Link disconnected. Retrying in 2 seconds...");
    appState.wsConnected = false;
    updateWsIndicator(false);
    setTimeout(initWebSocket, 2000);
  };
}

function updateWsIndicator(online) {
  const dot = document.getElementById("ws-dot");
  if (dot) {
    dot.className = online ? "status-dot online" : "status-dot offline";
  }
}

function handleWsMessage(msg) {
  switch (msg.type) {
    case "init":
      appState.ip = msg.ip || appState.ip;
      appState.port = msg.port || appState.port;
      updateTncConnectionState(msg.state || (msg.connected ? "connected" : "disconnected"), msg.ip, msg.port, msg.reboot_seconds);
      if (msg.stations) {
        msg.stations.forEach(updateStation);
      }
      if (msg.config) {
        populateConfigForm(msg.config);
      }
      if (msg.status) {
        updateTelemetryStatus(msg.status);
      }
      if (msg.weather) {
        updateWeatherStatus(msg.weather);
      }
      break;

    case "state_change":
      updateTncConnectionState(msg.state, msg.ip || appState.ip, msg.port || appState.port, msg.reboot_seconds);
      if (msg.config_state && appState.configState !== "dirty") {
        setConfigSyncState(msg.config_state);
      }
      break;

    case "connection":
      updateTncConnectionState(msg.state || (msg.connected ? "connected" : "disconnected"), msg.ip, msg.port);
      break;

    case "packet":
      logPacket(msg.packet);
      if (msg.station) {
        updateStation(msg.station);
      }
      if (msg.packet && msg.packet.weather) {
        updateWeatherStatus(msg.packet.weather);
      }
      break;

    case "weather":
      updateWeatherStatus(msg.weather);
      break;

    case "raw_line":
      logRawLine(msg.line, msg.timestamp);
      if (msg.status) {
        updateTelemetryStatus(msg.status);
      }
      break;

    case "config_update":
      if (appState.configState === "dirty") {
        appState.lastLoadedConfig = JSON.parse(JSON.stringify(msg.config));
        showToast("Device EEPROM updated in background (unsaved form edits preserved)", "info");
      } else {
        populateConfigForm(msg.config);
        if (msg.config_state) setConfigSyncState(msg.config_state);
      }
      if (msg.status) updateTelemetryStatus(msg.status);
      if (msg.weather) updateWeatherStatus(msg.weather);
      break;

    case "stations_cleared":
      heardStations = {};
      if (map) {
        Object.values(markers).forEach(m => map.removeLayer(m));
        Object.values(polylines).forEach(p => map.removeLayer(p));
      }
      markers = {};
      polylines = {};
      renderStationsTable();
      updateMapStats();
      showToast("Station history cleared", "info");
      break;
  }
}

function updateTncConnectionState(state, ip, port, rebootSeconds = null) {
  const previousState = appState.tncState;
  appState.tncState = state;
  appState.rebootSeconds = rebootSeconds;
  if (ip) appState.ip = ip;
  if (port) appState.port = port;

  const pill = document.getElementById("conn-pill");
  const btn = document.getElementById("btn-connect");
  const rebootBanner = document.getElementById("reboot-banner");
  const rebootMsg = document.getElementById("reboot-countdown-msg");
  const rebootFill = document.getElementById("reboot-progress-fill");

  const actionButtons = [
    document.getElementById("btn-read-config"),
    document.getElementById("btn-write-config"),
    document.getElementById("btn-cmd-reset"),
    document.getElementById("btn-cmd-install"),
    document.getElementById("btn-test-gate-rf"),
    document.getElementById("btn-test-gate-is"),
    document.getElementById("btn-test-wx-rf"),
    document.getElementById("btn-test-wx-is"),
  ];

  if (state === "connected") {
    pill.textContent = `Connected (${appState.ip}:${appState.port})`;
    pill.className = "pill pill-connected";
    btn.textContent = "Disconnect";
    btn.className = "btn btn-danger";
    btn.disabled = false;
    if (rebootBanner) rebootBanner.style.display = "none";
    actionButtons.forEach(b => { if (b) b.disabled = false; });

    if (previousState === "rebooting") {
      showToast("NET-51TNC reconnected! Device reboot completed.", "success");
    }
  } else if (state === "connecting") {
    pill.textContent = `Connecting (${appState.ip}:${appState.port})...`;
    pill.className = "pill pill-connecting";
    btn.textContent = "Connecting...";
    btn.className = "btn btn-secondary";
    btn.disabled = true;
    if (rebootBanner) rebootBanner.style.display = "none";
    actionButtons.forEach(b => { if (b) b.disabled = true; });
  } else if (state === "rebooting") {
    const sec = rebootSeconds || 8;
    pill.textContent = `Rebooting (${sec}s)`;
    pill.className = "pill pill-rebooting";
    btn.textContent = "Rebooting...";
    btn.className = "btn btn-secondary";
    btn.disabled = true;

    if (rebootBanner) {
      rebootBanner.style.display = "flex";
      if (rebootMsg) rebootMsg.textContent = `Re-establishing link and syncing in ${sec}s`;
      if (rebootFill) {
        const pct = Math.max(5, (sec / 8) * 100);
        rebootFill.style.width = `${pct}%`;
      }
    }
    actionButtons.forEach(b => { if (b) b.disabled = true; });
  } else {
    // Disconnected — disable TNC hardware action buttons
    pill.textContent = "Disconnected";
    pill.className = "pill pill-disconnected";
    btn.textContent = "Connect";
    btn.className = "btn btn-primary";
    btn.disabled = false;
    if (rebootBanner) rebootBanner.style.display = "none";
    actionButtons.forEach(b => { if (b) b.disabled = true; });
  }
}

function updateTelemetryStatus(status) {
  if (!status) return;
  if (status.voltage) {
    const el = document.getElementById("telemetry-voltage");
    if (el) el.textContent = status.voltage;
    const vBadge = document.getElementById("header-voltage");
    if (vBadge) {
      vBadge.textContent = status.voltage;
      vBadge.style.display = "inline-block";
    }
  }
  if (status.server_ip && status.server_port) {
    const el = document.getElementById("telemetry-server");
    if (el) el.textContent = `${status.server_ip}:${status.server_port}`;
  } else if (status.server_ip) {
    const el = document.getElementById("telemetry-server");
    if (el) el.textContent = status.server_ip;
  }
  if (status.server_status) {
    const el = document.getElementById("telemetry-login");
    if (el) el.textContent = status.server_status;
    const sBadge = document.getElementById("header-server-status");
    if (sBadge) {
      sBadge.textContent = status.server_status;
      sBadge.style.display = "inline-block";
    }
  }
}

function updateWeatherStatus(wx) {
  if (!wx) return;
  const setEl = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
  };
  setEl("wx-wind-dir", `${wx.wind_direction_deg}°`);
  setEl("wx-wind-speed", `${wx.wind_speed_ms} m/s (${wx.wind_speed_mph} mph)`);
  setEl("wx-wind-gust", `${wx.wind_gust_ms} m/s (${wx.wind_gust_mph} mph)`);
  setEl("wx-temp", `${wx.temp_c}°C (${wx.temp_f}°F)`);
  setEl("wx-humidity", `${wx.humidity_pct}%`);
  setEl("wx-pressure", `${wx.pressure_hpa} hPa`);
  setEl("wx-rain-1h", `${wx.rain_1h_mm} mm`);
  setEl("wx-rain-24h", `${wx.rain_24h_mm} mm`);
}

// Station & Map Management
function updateStation(station) {
  if (station.last_heard) {
    const ageSec = Math.max(0, (Date.now() - new Date(station.last_heard).getTime()) / 1000);
    if (ageSec > MAX_STATION_AGE_SEC) {
      return;
    }
  }
  heardStations[station.callsign] = station;

  // If this station is our own TNC gateway callsign and homeMarker is active, skip duplicate map marker
  const isHomeCall = appState.homeCallsign && station.callsign.toUpperCase() === appState.homeCallsign.toUpperCase();
  if (isHomeCall && homeMarker && markers[station.callsign]) {
    if (map) map.removeLayer(markers[station.callsign]);
    delete markers[station.callsign];
  }

  if (map && station.lat !== null && station.lon !== null && !(isHomeCall && homeMarker)) {
    const lat = Number(station.lat);
    const lon = Number(station.lon);
    const call = station.callsign;
    const isRf = !station.is_server;

    // Breadcrumb track polyline (controlled by #chk-show-tracks)
    const showTracks = getChk("chk-show-tracks", true);
    if (station.track && station.track.length > 1) {
      const latlngs = station.track.map(p => [p.lat, p.lon]);
      if (polylines[call]) {
        polylines[call].setLatLngs(latlngs);
      } else {
        polylines[call] = L.polyline(latlngs, {
          color: isRf ? "#10b981" : "#3b82f6",
          weight: 3,
          opacity: 0.65,
          dashArray: isRf ? null : "4, 6"
        });
      }
      if (showTracks && !map.hasLayer(polylines[call])) {
        polylines[call].addTo(map);
      } else if (!showTracks && map.hasLayer(polylines[call])) {
        map.removeLayer(polylines[call]);
      }
    }

    const markerClass = isRf ? "marker-bubble marker-rf" : "marker-bubble marker-is";
    const symMeta = getAprsSymbolMeta(station.symbol_table || "/", station.symbol_code || "&");
    const safeCall = escapeHtml(call);
    const icon = L.divIcon({
      className: "custom-aprs-marker",
      html: `<div class="${markerClass}" title="${escapeHtml(symMeta.name)} (${escapeHtml(symMeta.combo)})"><span>${symMeta.icon}</span><small>${safeCall}</small></div>`,
      iconSize: null,
      iconAnchor: [0, 0],
      popupAnchor: [0, -16],
    });

    if (markers[call]) {
      markers[call].setLatLng([lat, lon]);
      markers[call].setIcon(icon);
    } else {
      const marker = L.marker([lat, lon], { icon }).addTo(map);
      markers[call] = marker;
    }

    const freshnessTag = station.freshness === "active"
      ? '<span class="badge badge-active">🟢 Active</span>'
      : station.freshness === "recent"
      ? '<span class="badge badge-recent">🟡 Recent</span>'
      : '<span class="badge badge-stale">⚪ Stale</span>';

    markers[call].bindPopup(`
      <strong>${symMeta.icon} ${safeCall}</strong> ${isRf ? '<span class="badge badge-rf">RF</span>' : '<span class="badge badge-is">IS</span>'} ${freshnessTag}<br/>
      <b>Symbol:</b> ${escapeHtml(symMeta.name)} (<code>${escapeHtml(symMeta.combo)}</code>)<br/>
      <b>Coords:</b> ${lat.toFixed(4)}, ${lon.toFixed(4)}<br/>
      <b>Path:</b> ${escapeHtml(station.path || "DIRECT")}<br/>
      <b>Packets:</b> ${Number(station.packet_count || 1)}<br/>
      <b>Comment:</b> ${escapeHtml(station.comment || "N/A")}<br/>
      <b>Last heard:</b> ${escapeHtml(formatLocalTime(station.last_heard, true))}<br/>
      <div style="margin-top:6px;">
        <a href="https://aprs.fi/#!call=a%2F${encodeURIComponent(call)}" target="_blank" rel="noopener noreferrer" style="color:#60a5fa; font-size:0.8rem; text-decoration:none;">View on aprs.fi ↗</a>
      </div>
    `);
  }

  renderStationsTable();
  updateMapStats();
}

function updateHomeStationMarker(cfg, recenter = true) {
  if (!map || !cfg) return;
  const latStr = aprsToDecimal(cfg.gateway_lat, false);
  const lonStr = aprsToDecimal(cfg.gateway_lon, true);
  if (latStr === null || lonStr === null) return;

  const lat = parseFloat(latStr);
  const lon = parseFloat(lonStr);
  homeCoords = [lat, lon];

  const baseCall = (cfg.callsign || "NOCALL").trim().toUpperCase();
  appState.stationTimeZone = resolveStationTimeZone(lat, lon, baseCall);
  updateHeaderClock();
  renderStationsTable();
  const ssid = cfg.gateway_ssid !== undefined ? cfg.gateway_ssid : (cfg.ssid !== undefined ? cfg.ssid : 10);
  const fullCall = ssid > 0 ? `${baseCall}-${ssid}` : baseCall;
  appState.homeCallsign = fullCall;
  if (markers[fullCall]) {
    map.removeLayer(markers[fullCall]);
    delete markers[fullCall];
  }
  const safeCall = escapeHtml(fullCall);
  const symMeta = getAprsSymbolMeta(cfg.gateway_symbol_table || "/", cfg.gateway_symbol_code || "r");

  const icon = L.divIcon({
    className: "custom-aprs-marker",
    html: `<div class="marker-bubble marker-home" title="My TNC Gateway (${safeCall} — ${escapeHtml(symMeta.name)})"><span>${symMeta.icon}</span><small>${safeCall}</small></div>`,
    iconSize: null,
    iconAnchor: [0, 0],
    popupAnchor: [0, -28],
  });

  if (homeMarker) {
    homeMarker.setLatLng([lat, lon]);
    homeMarker.setIcon(icon);
  } else {
    homeMarker = L.marker([lat, lon], { icon, zIndexOffset: 900 }).addTo(map);
  }

  const isSec = cfg.gateway_beacon_is_sec !== undefined ? cfg.gateway_beacon_is_sec : 120;
  const rfSec = cfg.gateway_beacon_rf_sec !== undefined ? cfg.gateway_beacon_rf_sec : 0;

  homeMarker.bindPopup(`
    <strong>${symMeta.icon} ${safeCall}</strong> <span class="badge badge-active">My TNC Gateway</span><br/>
    <b>APRS Pos:</b> ${escapeHtml(cfg.gateway_lat)} / ${escapeHtml(cfg.gateway_lon)}<br/>
    <b>Decimal:</b> ${lat.toFixed(5)}°, ${lon.toFixed(5)}°<br/>
    <b>Symbol:</b> ${escapeHtml(symMeta.name)} (<code>${escapeHtml(symMeta.combo)}</code>) | <b>Path:</b> ${escapeHtml(cfg.gateway_path || "WIDE1-1")}<br/>
    <b>Beacons:</b> APRS-IS ${isSec > 0 ? isSec + "s" : "Off"} | RF ${rfSec > 0 ? rfSec + "s" : "Off"}<br/>
    <b>Comment:</b> ${escapeHtml(cfg.gateway_comment || "(none)")}<br/>
    <div style="margin-top:6px;">
      <a href="https://aprs.fi/#!call=a%2F${encodeURIComponent(fullCall)}" target="_blank" rel="noopener noreferrer" style="color:#60a5fa; font-size:0.8rem; text-decoration:none;">View on aprs.fi ↗</a>
    </div>
  `);

  if (recenter) {
    map.setView([lat, lon], 11);
  }
  updateMapStats();
}

function updateMapStats() {
  const positionedCount = Object.keys(markers).length + (homeMarker ? 1 : 0);
  const statsEl = document.getElementById("map-stats");
  if (statsEl) statsEl.textContent = `${positionedCount} positioned station${positionedCount === 1 ? "" : "s"}`;
  const countEl = document.getElementById("station-count");
  if (countEl) countEl.textContent = Object.keys(heardStations).length;
}

function renderStationsTable() {
  const tbody = document.getElementById("stations-tbody");
  const filter = (document.getElementById("station-filter").value || "").toLowerCase();
  const rfOnly = document.getElementById("chk-rf-only") && document.getElementById("chk-rf-only").checked;
  const freshnessFilter = (document.getElementById("station-freshness-filter") || {}).value || "all";

  const stations = Object.values(heardStations).filter(s => {
    if (rfOnly && s.is_server) return false;
    if (freshnessFilter !== "all" && s.freshness !== freshnessFilter) return false;
    return s.callsign.toLowerCase().includes(filter) ||
           (s.path && s.path.toLowerCase().includes(filter)) ||
           (s.comment && s.comment.toLowerCase().includes(filter));
  });

  if (stations.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" class="text-muted text-center">No matching stations heard.</td></tr>';
    return;
  }

  stations.sort((a, b) => new Date(b.last_heard) - new Date(a.last_heard));

  let html = "";
  for (const s of stations) {
    const coords = (s.lat !== null && s.lon !== null)
      ? `${Number(s.lat).toFixed(4)}, ${Number(s.lon).toFixed(4)}`
      : '<span class="text-muted">No Position</span>';

    const sourceBadge = s.is_server
      ? '<span class="badge badge-is">IS</span>'
      : '<span class="badge badge-rf">RF</span>';

    const freshnessBadge = s.freshness === "active"
      ? '<span class="badge badge-active">🟢 Active</span>'
      : s.freshness === "recent"
      ? '<span class="badge badge-recent">🟡 Recent</span>'
      : '<span class="badge badge-stale">⚪ Stale</span>';

    const timeStr = s.last_heard ? formatLocalTime(s.last_heard, true) : "";
    const symMeta = getAprsSymbolMeta(s.symbol_table || "/", s.symbol_code || "&");

    html += `
      <tr>
        <td><span title="${escapeHtml(symMeta.name)} (${escapeHtml(symMeta.combo)})" style="margin-right:5px;">${symMeta.icon}</span><strong>${escapeHtml(s.callsign)}</strong></td>
        <td>${freshnessBadge}</td>
        <td>${sourceBadge}</td>
        <td>${coords}</td>
        <td>${escapeHtml(timeStr)}</td>
        <td>${Number(s.packet_count || 0)}</td>
        <td><small>${escapeHtml(s.path || "")}</small></td>
        <td>${escapeHtml(s.comment || "")}</td>
        <td>
          <div style="display:flex; gap:4px; align-items:center;">
            ${s.lat !== null ? `<button class="btn btn-secondary btn-sm btn-center-map" data-callsign="${escapeHtml(s.callsign)}">Map</button>` : ''}
            <a href="https://aprs.fi/#!call=a%2F${encodeURIComponent(s.callsign)}" target="_blank" rel="noopener noreferrer" class="btn btn-secondary btn-sm" title="View on aprs.fi" style="text-decoration:none; display:inline-flex; align-items:center; font-size:0.75rem;">aprs.fi ↗</a>
          </div>
        </td>
      </tr>
    `;
  }

  tbody.innerHTML = html;
}

window.centerStation = function(callsign) {
  const s = heardStations[callsign];
  if (s && s.lat !== null && s.lon !== null && map) {
    document.querySelector('.tab-btn[data-tab="tab-map"]').click();
    map.setView([s.lat, s.lon], 13);
    if (markers[callsign]) {
      markers[callsign].openPopup();
    }
  }
};

// Console Logging (Bounded to MAX_CONSOLE_LINES)
function trimConsoleBox(box) {
  while (box.childElementCount > MAX_CONSOLE_LINES) {
    box.removeChild(box.firstElementChild);
  }
}

function logPacket(packet) {
  const box = document.getElementById("packet-console");
  const hideServer = document.getElementById("chk-hide-server").checked;
  if (hideServer && packet.is_server) return;

  const lineDiv = document.createElement("div");
  lineDiv.className = "console-line";

  const timeSpan = document.createElement("span");
  timeSpan.className = "console-time";
  timeSpan.textContent = `[${formatLocalTime(new Date(), false)}]`;

  const tagSpan = document.createElement("span");
  tagSpan.className = packet.is_server ? "console-is" : "console-rf";
  tagSpan.textContent = packet.is_server ? "[IS] " : "[RF] ";

  const textSpan = document.createElement("span");
  textSpan.textContent = packet.raw;

  lineDiv.appendChild(timeSpan);
  lineDiv.appendChild(tagSpan);
  lineDiv.appendChild(textSpan);
  box.appendChild(lineDiv);
  trimConsoleBox(box);

  if (document.getElementById("chk-autoscroll").checked) {
    box.scrollTop = box.scrollHeight;
  }
}

function logRawLine(line, timestamp) {
  const box = document.getElementById("packet-console");
  const lineDiv = document.createElement("div");
  lineDiv.className = "console-line console-raw";
  lineDiv.textContent = `[${formatLocalTime(timestamp || new Date(), false)}] ${line}`;
  box.appendChild(lineDiv);
  trimConsoleBox(box);

  if (document.getElementById("chk-autoscroll").checked) {
    box.scrollTop = box.scrollHeight;
  }
}

// Configuration Form & Dirty Tracking State Machine
const getVal = (id, def = "") => {
  const el = document.getElementById(id);
  return el ? el.value : def;
};
const setVal = (id, val) => {
  const el = document.getElementById(id);
  if (el) el.value = val !== undefined && val !== null ? val : "";
};
const getChk = (id, def = false) => {
  const el = document.getElementById(id);
  return el ? el.checked : def;
};
const setChk = (id, val) => {
  const el = document.getElementById(id);
  if (el) el.checked = !!val;
};
const intOrDefault = (rawVal, defVal) => {
  const parsed = parseInt(rawVal, 10);
  return Number.isNaN(parsed) ? defVal : parsed;
};

function setConfigSyncState(state) {
  appState.configState = state;
  const badge = document.getElementById("config-sync-badge");
  const revertBtn = document.getElementById("btn-revert-config");
  if (!badge) return;

  if (state === "synced") {
    badge.className = "badge badge-sync-synced";
    badge.textContent = "● Synced with EEPROM";
    if (revertBtn) revertBtn.style.display = "none";
  } else if (state === "dirty") {
    badge.className = "badge badge-sync-dirty";
    badge.textContent = "● Unsaved Changes (Modified)";
    if (revertBtn) revertBtn.style.display = "inline-block";
  } else if (state === "saving") {
    badge.className = "badge badge-sync-saving";
    badge.textContent = "● Writing to EEPROM...";
    if (revertBtn) revertBtn.style.display = "none";
  } else if (state === "syncing") {
    badge.className = "badge badge-sync-saving";
    badge.textContent = "● Reading from TNC (#HELLO)...";
    if (revertBtn) revertBtn.style.display = "none";
  } else {
    badge.className = "badge badge-sync-unknown";
    badge.textContent = "● EEPROM: Unknown";
    if (revertBtn) revertBtn.style.display = "none";
  }
}

function renderGatewayIconPicker() {
  const container = document.getElementById("gw-icon-picker");
  if (!container) return;

  container.innerHTML = "";
  APRS_SYMBOL_CATALOG.forEach((item) => {
    const combo = `${item.table}${item.code}`;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "aprs-icon-tile";
    btn.dataset.table = item.table;
    btn.dataset.code = item.code;
    btn.dataset.combo = combo;
    btn.title = `${item.name} (Table '${item.table}', Code '${item.code}')`;
    btn.innerHTML = `
      <span class="aprs-tile-emoji">${item.icon}</span>
      <span class="aprs-tile-name">${escapeHtml(item.short)}</span>
      <span class="aprs-tile-code">${escapeHtml(combo)}</span>
    `;
    btn.addEventListener("click", () => {
      setVal("cfg-gw-sym-table", item.table);
      setVal("cfg-gw-sym-code", item.code);
      syncGatewaySymbolPreset();
      checkConfigDirty();
      updateHomeStationMarker(gatherConfigForm(), false);
    });
    container.appendChild(btn);
  });
  syncGatewaySymbolPreset();
}

function syncGatewaySymbolPreset() {
  const tbl = getVal("cfg-gw-sym-table", "/") || "/";
  const code = getVal("cfg-gw-sym-code", "r") || "r";
  const combo = `${tbl}${code}`;
  const meta = getAprsSymbolMeta(tbl, code);

  const labelEl = document.getElementById("gw-selected-icon-label");
  if (labelEl) {
    labelEl.textContent = `${meta.icon} ${meta.name} (${meta.combo})`;
  }

  const container = document.getElementById("gw-icon-picker");
  if (container) {
    container.querySelectorAll(".aprs-icon-tile").forEach((tile) => {
      const isMatch = tile.dataset.combo === combo;
      tile.classList.toggle("active", isMatch);
      tile.setAttribute("aria-pressed", isMatch ? "true" : "false");
    });
  }
}

function updateGatewayBeaconPreview() {
  const previewEl = document.getElementById("gw-packet-preview");
  const counterEl = document.getElementById("gw-comment-counter");
  const comment = getVal("cfg-gw-comment", "");
  if (counterEl) {
    counterEl.textContent = `${comment.length} / 70 chars`;
  }
  if (!previewEl) return;

  const call = (getVal("cfg-callsign", "NOCALL").trim().toUpperCase()) || "NOCALL";
  const ssid = intOrDefault(getVal("cfg-gw-ssid", getVal("cfg-ssid", "10")), 10);
  const src = ssid > 0 ? `${call}-${ssid}` : call;
  const rawPath = getVal("cfg-gw-path", "WIDE1-1");
  const pathPart = rawPath && rawPath !== "- - -" ? `,${rawPath}` : "";
  const pktType = getVal("cfg-gw-pkt-type", "!") || "!";
  const lat = (getVal("cfg-gw-lat", "").trim() || "0000.00N").padEnd(8, " ");
  const symTable = getVal("cfg-gw-sym-table", "/") || "/";
  const lon = (getVal("cfg-gw-lon", "").trim() || "00000.00E").padEnd(9, " ");
  const symCode = getVal("cfg-gw-sym-code", "r") || "r";
  const voltTag = getChk("cfg-gw-voltage") ? " 13.8V" : "";

  previewEl.textContent = `${src}>APET51${pathPart}:${pktType}${lat}${symTable}${lon}${symCode}${comment}${voltTag}`;
}

function aprsToDecimal(aprsStr, isLon) {
  const s = (aprsStr || "").trim().toUpperCase();
  const re = isLon ? /^(\d{3})(\d{2}\.\d{2})([EW])$/ : /^(\d{2})(\d{2}\.\d{2})([NS])$/;
  const m = s.match(re);
  if (!m) return null;
  const deg = parseInt(m[1], 10);
  const min = parseFloat(m[2]);
  if (min >= 60) return null;
  let dec = deg + min / 60.0;
  if (m[3] === "S" || m[3] === "W") dec = -dec;
  return dec.toFixed(5);
}

function decimalToAprs(decVal, isLon) {
  const num = parseFloat(decVal);
  if (Number.isNaN(num)) return null;
  const maxAbs = isLon ? 180 : 90;
  if (Math.abs(num) > maxAbs) return null;
  const hemi = isLon ? (num >= 0 ? "E" : "W") : (num >= 0 ? "N" : "S");
  const absVal = Math.abs(num);
  let deg = Math.floor(absVal);
  let min = (absVal - deg) * 60.0;
  if (min >= 59.995) {
    deg += 1;
    min = 0.0;
  }
  const degStr = String(deg).padStart(isLon ? 3 : 2, "0");
  const minStr = min.toFixed(2).padStart(5, "0");
  return `${degStr}${minStr}${hemi}`;
}

function checkConfigDirty() {
  updateGatewayBeaconPreview();
  if (!appState.lastLoadedConfig) return;
  const current = gatherConfigForm();
  let dirty = false;
  for (const [key, val] of Object.entries(current)) {
    if (appState.lastLoadedConfig[key] !== undefined && appState.lastLoadedConfig[key] !== val) {
      dirty = true;
      break;
    }
  }
  setConfigSyncState(dirty ? "dirty" : "synced");
}

function initConfigDirtyTracking() {
  const formArea = document.getElementById("tab-config");
  if (!formArea) return;
  formArea.addEventListener("input", checkConfigDirty);
  formArea.addEventListener("change", checkConfigDirty);

  // Sync aria-invalid and dynamic error descriptions after interaction
  const linkError = (el, shown) => {
    const errorId = el.dataset ? el.dataset.error : null;
    if (!errorId) return;
    const ids = (el.getAttribute("aria-describedby") || "").split(/\s+/).filter((id) => id && id !== errorId);
    if (shown) ids.push(errorId);
    if (ids.length) el.setAttribute("aria-describedby", ids.join(" "));
    else el.removeAttribute("aria-describedby");
  };

  const syncFieldValidity = (el) => {
    if (!el || !el.matches || !el.matches("input, select, textarea")) return;
    const isInvalid = !el.validity.valid;
    el.setAttribute("aria-invalid", isInvalid ? "true" : "false");
    el.classList.toggle("user-invalid-fallback", isInvalid);
    linkError(el, isInvalid);
  };

  formArea.querySelectorAll("input, select, textarea").forEach((el) => {
    el.setAttribute("aria-invalid", "false");
  });

  formArea.addEventListener("blur", (e) => {
    if (e.target && e.target.matches && e.target.matches("input, select, textarea")) {
      syncFieldValidity(e.target);
    }
  }, true);

  formArea.addEventListener("input", (e) => {
    if (e.target && e.target.getAttribute && e.target.getAttribute("aria-invalid") === "true") {
      syncFieldValidity(e.target);
    }
  });

  formArea.addEventListener("invalid", (e) => {
    syncFieldValidity(e.target);
  }, true);

  const symTblEl = document.getElementById("cfg-gw-sym-table");
  const symCodeEl = document.getElementById("cfg-gw-sym-code");
  if (symTblEl) symTblEl.addEventListener("input", syncGatewaySymbolPreset);
  if (symCodeEl) symCodeEl.addEventListener("input", syncGatewaySymbolPreset);

  // Sync Gateway Beacon SSID (offset 135) automatically when Station Identity SSID (offset 7) changes
  const mainSsidEl = document.getElementById("cfg-ssid");
  if (mainSsidEl) {
    const syncGwSsid = () => {
      setVal("cfg-gw-ssid", mainSsidEl.value);
      updateGatewayBeaconPreview();
      updateHomeStationMarker(gatherConfigForm(), false);
    };
    mainSsidEl.addEventListener("input", syncGwSsid);
    mainSsidEl.addEventListener("change", syncGwSsid);
  }

  const toggleCalcBtn = document.getElementById("btn-toggle-coord-calc");
  const calcDrawer = document.getElementById("coord-calc-drawer");
  if (toggleCalcBtn && calcDrawer) {
    toggleCalcBtn.addEventListener("click", () => {
      const open = calcDrawer.style.display !== "none";
      calcDrawer.style.display = open ? "none" : "block";
      toggleCalcBtn.setAttribute("aria-expanded", open ? "false" : "true");
      if (!open) {
        const curLat = aprsToDecimal(getVal("cfg-gw-lat"), false);
        const curLon = aprsToDecimal(getVal("cfg-gw-lon"), true);
        if (curLat !== null) setVal("calc-dec-lat", curLat);
        if (curLon !== null) setVal("calc-dec-lon", curLon);
      }
    });
  }

  const applyDecBtn = document.getElementById("btn-apply-dec-coords");
  if (applyDecBtn) {
    applyDecBtn.addEventListener("click", () => {
      const aprsLat = decimalToAprs(getVal("calc-dec-lat"), false);
      const aprsLon = decimalToAprs(getVal("calc-dec-lon"), true);
      if (!aprsLat || !aprsLon) {
        showToast("Enter valid decimal coordinates (Lat -90..90, Lon -180..180)", "warning");
        return;
      }
      setVal("cfg-gw-lat", aprsLat);
      setVal("cfg-gw-lon", aprsLon);
      checkConfigDirty();
      updateHomeStationMarker(gatherConfigForm(), true);
      showToast(`Converted to APRS coordinates: ${aprsLat} / ${aprsLon}`, "success");
    });
  }

  const pickMapBtn = document.getElementById("btn-pick-map-coords");
  if (pickMapBtn) {
    pickMapBtn.addEventListener("click", startMapCoordPicker);
  }

  const cancelPickBtn = document.getElementById("btn-cancel-pick-map");
  if (cancelPickBtn) {
    cancelPickBtn.addEventListener("click", () => {
      stopMapCoordPicker();
      const cfgTabBtn = document.querySelector('.tab-btn[data-tab="tab-config"]');
      if (cfgTabBtn) cfgTabBtn.click();
    });
  }

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && appState.pickingCoords) {
      stopMapCoordPicker();
    }
  });

  const revertBtn = document.getElementById("btn-revert-config");
  if (revertBtn) {
    revertBtn.addEventListener("click", () => {
      if (appState.lastLoadedConfig) {
        populateConfigForm(appState.lastLoadedConfig);
        setConfigSyncState("synced");
        showToast("Form restored to device EEPROM values", "info");
      }
    });
  }
}

function populateConfigForm(cfg) {
  if (!cfg) return;
  appState.lastLoadedConfig = JSON.parse(JSON.stringify(cfg));

  // Station ID
  setVal("cfg-callsign", cfg.callsign || "");
  setVal("cfg-ssid", cfg.ssid !== undefined ? cfg.ssid : 10);
  setVal("cfg-passcode", cfg.passcode || "");

  // Network
  setVal("cfg-ip", cfg.ip_address || "192.168.1.120");
  setVal("cfg-mask", cfg.netmask || "255.255.255.0");
  setVal("cfg-gw", cfg.gateway || "192.168.1.1");
  setVal("cfg-mac", cfg.mac_address || "");

  // APRS-IS Servers & Features
  setVal("cfg-server-a", cfg.server_a || "85.188.1.32");
  setVal("cfg-server-b", cfg.server_b || "0.0.0.0");
  setVal("cfg-server-c", cfg.server_c || "0.0.0.0");
  setVal("cfg-server-mode", cfg.server_mode || "rotate");
  setVal("cfg-filter", cfg.aprs_filter || "");
  setVal("cfg-lcd-type", cfg.lcd_type || "Close");
  setChk("cfg-allow-is-rf", cfg.allow_is_to_rf);
  setChk("cfg-timeout-restart", cfg.timeout_restart !== undefined ? cfg.timeout_restart : true);
  setChk("cfg-hellocq", cfg.hellocq_enabled);
  setChk("cfg-pm25-en", cfg.pm25_enabled);

  // Modem & Radio
  setVal("cfg-baud-mode", cfg.baud_mode || "Clever 4800");
  setVal("cfg-tx-level", cfg.tx_level || "YAESU_LOW");
  setVal("cfg-ptt-delay", cfg.ptt_delay_code !== undefined ? cfg.ptt_delay_code : 1);
  setVal("cfg-min-tx-interval", cfg.min_tx_interval_code !== undefined ? cfg.min_tx_interval_code : 0);
  setVal("cfg-tx-waiting", cfg.tx_waiting_code !== undefined ? cfg.tx_waiting_code : 1);
  setChk("cfg-digi-wide1", cfg.digi_wide1);
  setChk("cfg-digi-wide2", cfg.digi_wide2);
  setChk("cfg-insert-gw", cfg.insert_gateway_call);
  setChk("cfg-wait-idle", cfg.wait_channel_idle !== undefined ? cfg.wait_channel_idle : true);
  setChk("cfg-sql-detect", cfg.sql_hardware_detect);
  setChk("cfg-rf-lcd", cfg.rf_lcd_display !== undefined ? cfg.rf_lcd_display : false);

  // Gateway Beacon
  setVal("cfg-gw-is-sec", cfg.gateway_beacon_is_sec !== undefined ? cfg.gateway_beacon_is_sec : 120);
  setVal("cfg-gw-rf-sec", cfg.gateway_beacon_rf_sec !== undefined ? cfg.gateway_beacon_rf_sec : 0);
  setVal("cfg-gw-path", cfg.gateway_path || "WIDE1-1");
  setVal("cfg-gw-ssid", cfg.gateway_ssid !== undefined ? cfg.gateway_ssid : (cfg.ssid !== undefined ? cfg.ssid : 10));
  setVal("cfg-gw-pkt-type", cfg.gateway_packet_type || "!");
  setVal("cfg-gw-lat", cfg.gateway_lat || "");
  setVal("cfg-gw-sym-table", cfg.gateway_symbol_table || "/");
  setVal("cfg-gw-lon", cfg.gateway_lon || "");
  setVal("cfg-gw-sym-code", cfg.gateway_symbol_code || "r");
  setVal("cfg-gw-comment", cfg.gateway_comment || "");
  setChk("cfg-gw-voltage", cfg.gateway_report_voltage);
  syncGatewaySymbolPreset();

  // Weather Beacon
  setVal("cfg-wx-is-sec", cfg.weather_beacon_is_sec !== undefined ? cfg.weather_beacon_is_sec : 0);
  setVal("cfg-wx-rf-sec", cfg.weather_beacon_rf_sec !== undefined ? cfg.weather_beacon_rf_sec : 0);
  setVal("cfg-wx-path", cfg.weather_path || "WIDE1-1");
  setVal("cfg-wx-ssid", cfg.weather_ssid !== undefined ? cfg.weather_ssid : 13);
  setVal("cfg-wx-pkt-type", cfg.weather_packet_type || "=");
  setVal("cfg-wx-lat", cfg.weather_lat || "");
  setVal("cfg-wx-sym-table", cfg.weather_symbol_table || "/");
  setVal("cfg-wx-lon", cfg.weather_lon || "");
  setVal("cfg-wx-sym-code", cfg.weather_symbol_code || "_");
  setVal("cfg-wx-comment", cfg.weather_comment || "");
  setChk("cfg-wx-voltage", cfg.weather_report_voltage);
  setChk("cfg-wx-gps-coords", cfg.weather_use_gps_coords);
  setChk("cfg-wx-lcd-display", cfg.weather_lcd_display);

  // GPS & Tracking
  setVal("cfg-gps-baud", cfg.gps_baud ? String(cfg.gps_baud) : "4800");
  setVal("cfg-gps-interval", cfg.gps_beacon_interval !== undefined ? cfg.gps_beacon_interval : 0);
  setVal("cfg-gps-mode", cfg.gps_beacon_mode || "Close");
  setVal("cfg-gps-ssid", cfg.gps_ssid !== undefined ? cfg.gps_ssid : 9);
  setVal("cfg-gps-path", cfg.gps_path || "- - -");
  setVal("cfg-gps-pkt-type", cfg.gps_packet_type || "!");
  setVal("cfg-gps-sym-table", cfg.gps_symbol_table || "/");
  setVal("cfg-gps-sym-code", cfg.gps_symbol_code || "#");
  setChk("cfg-gps-voltage", cfg.report_voltage);
  setChk("cfg-gps-tracks", cfg.gps_tracks);
  setChk("cfg-gps-lcd", cfg.gps_lcd_display);
  setChk("cfg-gps-offset-en", cfg.gps_offset_enabled);
  setVal("cfg-gps-lat-sign", cfg.gps_lat_offset_neg ? "-" : "+");
  setVal("cfg-gps-lat-val", cfg.gps_lat_offset !== undefined ? cfg.gps_lat_offset : 0);
  setVal("cfg-gps-lon-sign", cfg.gps_lon_offset_neg ? "-" : "+");
  setVal("cfg-gps-lon-val", cfg.gps_lon_offset !== undefined ? cfg.gps_lon_offset : 0);
  setVal("cfg-gps-custom", cfg.gps_custom_info || "");

  updateGatewayBeaconPreview();
  updateHomeStationMarker(cfg, true);
  syncQuickSetupFromConfig(cfg);
  setConfigSyncState("synced");
}

function gatherConfigForm() {
  const ssid = intOrDefault(getVal("cfg-ssid", "10"), 10);
  return {
    callsign: getVal("cfg-callsign").trim().toUpperCase(),
    ssid,
    passcode: getVal("cfg-passcode").trim(),

    ip_address: getVal("cfg-ip", "192.168.1.120").trim(),
    netmask: getVal("cfg-mask", "255.255.255.0").trim(),
    gateway: getVal("cfg-gw", "192.168.1.1").trim(),

    server_a: getVal("cfg-server-a", "85.188.1.32").trim(),
    server_b: getVal("cfg-server-b", "0.0.0.0").trim(),
    server_c: getVal("cfg-server-c", "0.0.0.0").trim(),
    server_mode: getVal("cfg-server-mode", "rotate"),
    aprs_filter: getVal("cfg-filter").trim(),
    lcd_type: getVal("cfg-lcd-type", "Close"),
    allow_is_to_rf: getChk("cfg-allow-is-rf"),
    timeout_restart: getChk("cfg-timeout-restart", true),
    hellocq_enabled: getChk("cfg-hellocq"),
    pm25_enabled: getChk("cfg-pm25-en"),

    baud_mode: getVal("cfg-baud-mode", "Clever 4800"),
    tx_level: getVal("cfg-tx-level", "YAESU_LOW"),
    ptt_delay_code: intOrDefault(getVal("cfg-ptt-delay", "1"), 1),
    min_tx_interval_code: intOrDefault(getVal("cfg-min-tx-interval", "0"), 0),
    tx_waiting_code: intOrDefault(getVal("cfg-tx-waiting", "1"), 1),
    digi_wide1: getChk("cfg-digi-wide1"),
    digi_wide2: getChk("cfg-digi-wide2"),
    insert_gateway_call: getChk("cfg-insert-gw"),
    wait_channel_idle: getChk("cfg-wait-idle", true),
    sql_hardware_detect: getChk("cfg-sql-detect"),
    rf_lcd_display: getChk("cfg-rf-lcd"),

    gateway_beacon_is_sec: intOrDefault(getVal("cfg-gw-is-sec", "120"), 120),
    gateway_beacon_rf_sec: intOrDefault(getVal("cfg-gw-rf-sec", "0"), 0),
    gateway_path: getVal("cfg-gw-path", "WIDE1-1"),
    gateway_ssid: intOrDefault(getVal("cfg-gw-ssid", String(ssid)), ssid),
    gateway_packet_type: getVal("cfg-gw-pkt-type", "!") || "!",
    gateway_lat: getVal("cfg-gw-lat").trim().toUpperCase(),
    gateway_symbol_table: getVal("cfg-gw-sym-table", "/") || "/",
    gateway_lon: getVal("cfg-gw-lon").trim().toUpperCase(),
    gateway_symbol_code: getVal("cfg-gw-sym-code", "r") || "r",
    gateway_comment: getVal("cfg-gw-comment"),
    gateway_report_voltage: getChk("cfg-gw-voltage"),

    weather_beacon_is_sec: intOrDefault(getVal("cfg-wx-is-sec", "0"), 0),
    weather_beacon_rf_sec: intOrDefault(getVal("cfg-wx-rf-sec", "0"), 0),
    weather_path: getVal("cfg-wx-path", "WIDE1-1"),
    weather_ssid: intOrDefault(getVal("cfg-wx-ssid", "13"), 13),
    weather_packet_type: getVal("cfg-wx-pkt-type", "=") || "=",
    weather_lat: getVal("cfg-wx-lat").trim().toUpperCase(),
    weather_symbol_table: getVal("cfg-wx-sym-table", "/") || "/",
    weather_lon: getVal("cfg-wx-lon").trim().toUpperCase(),
    weather_symbol_code: getVal("cfg-wx-sym-code", "_") || "_",
    weather_comment: getVal("cfg-wx-comment"),
    weather_report_voltage: getChk("cfg-wx-voltage"),
    weather_use_gps_coords: getChk("cfg-wx-gps-coords"),
    weather_lcd_display: getChk("cfg-wx-lcd-display"),

    gps_baud: intOrDefault(getVal("cfg-gps-baud", "4800"), 4800),
    gps_beacon_interval: intOrDefault(getVal("cfg-gps-interval", "0"), 0),
    gps_beacon_mode: getVal("cfg-gps-mode", "Close"),
    gps_ssid: intOrDefault(getVal("cfg-gps-ssid", "9"), 9),
    gps_path: getVal("cfg-gps-path", "- - -"),
    gps_packet_type: getVal("cfg-gps-pkt-type", "!") || "!",
    gps_symbol_table: getVal("cfg-gps-sym-table", "/") || "/",
    gps_symbol_code: getVal("cfg-gps-sym-code", "#") || "#",
    report_voltage: getChk("cfg-gps-voltage"),
    gps_tracks: getChk("cfg-gps-tracks"),
    gps_lcd_display: getChk("cfg-gps-lcd"),
    gps_offset_enabled: getChk("cfg-gps-offset-en"),
    gps_lat_offset_neg: getVal("cfg-gps-lat-sign") === "-",
    gps_lat_offset: intOrDefault(getVal("cfg-gps-lat-val", "0"), 0),
    gps_lon_offset_neg: getVal("cfg-gps-lon-sign") === "-",
    gps_lon_offset: intOrDefault(getVal("cfg-gps-lon-val", "0"), 0),
    gps_custom_info: getVal("cfg-gps-custom"),
  };
}

// User Actions & Buttons
function initControls() {
  const stationsTbody = document.getElementById("stations-tbody");
  if (stationsTbody) {
    stationsTbody.addEventListener("click", (e) => {
      const btn = e.target.closest(".btn-center-map");
      if (btn && btn.dataset.callsign) {
        window.centerStation(btn.dataset.callsign);
      }
    });
  }

  document.getElementById("btn-connect").addEventListener("click", async () => {
    const ip = document.getElementById("tnc-ip").value.trim();
    const port = parseInt(document.getElementById("tnc-port").value, 10) || 60000;

    if (appState.tncState === "connected") {
      const res = await fetch("/api/disconnect", { method: "POST" });
      if (res.ok) {
        showToast("Disconnected from NET-51TNC", "info");
      }
    } else {
      try {
        updateTncConnectionState("connecting", ip, port);
        const res = await fetch("/api/connect", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ip, port })
        });
        if (!res.ok) {
          const err = await res.json();
          updateTncConnectionState("disconnected", ip, port);
          showToast(`Connection failed: ${err.detail || res.statusText}`, "error");
        } else {
          showToast(`Connected to NET-51TNC at ${ip}:${port}`, "success");
        }
      } catch (e) {
        updateTncConnectionState("disconnected", ip, port);
        showToast(`Connection error: ${e.message}`, "error");
      }
    }
  });

  document.getElementById("btn-read-config").addEventListener("click", async () => {
    const btn = document.getElementById("btn-read-config");
    btn.disabled = true;
    setConfigSyncState("syncing");
    try {
      const res = await fetch("/api/config");
      if (res.ok) {
        const cfg = await res.json();
        populateConfigForm(cfg);
        showToast("Configuration read successfully from EEPROM (#HELLO)", "success");
      } else {
        const err = await res.json();
        showToast(`Failed to read configuration: ${err.detail || res.statusText}`, "error");
        setConfigSyncState("unknown");
      }
    } catch (e) {
      showToast(`Error: ${e.message}`, "error");
      setConfigSyncState("unknown");
    } finally {
      btn.disabled = appState.tncState !== "connected";
    }
  });

  const handleSaveConfig = async (e) => {
    if (e) e.preventDefault();
    const configForm = document.getElementById("config-form");
    if (configForm && !configForm.checkValidity()) {
      const firstInvalid = configForm.querySelector(":invalid");
      if (firstInvalid) {
        firstInvalid.focus();
      }
      showToast("Please fix the highlighted configuration fields before saving.", "warning");
      return;
    }

    const confirmed = await showConfirm(
      "Save parameters into NET-51TNC EEPROM? The hardware will restart and re-establish connection automatically.",
      "Save EEPROM Configuration"
    );
    if (!confirmed) return;

    const btn = document.getElementById("btn-write-config");
    btn.disabled = true;
    setConfigSyncState("saving");

    const cfg = gatherConfigForm();
    try {
      const res = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cfg)
      });
      if (res.ok) {
        const resJson = await res.json();
        if (resJson.config) {
          populateConfigForm(resJson.config);
        }
        showToast(resJson.message || "Saved successfully! TNC restarting.", "success");
        updateTncConnectionState("rebooting", appState.ip, appState.port, resJson.reboot_seconds || 8);
      } else {
        const err = await res.json();
        showToast(`Failed to save configuration: ${err.detail || res.statusText}`, "error");
        setConfigSyncState("dirty");
      }
    } catch (err) {
      showToast(`Error: ${err.message}`, "error");
      setConfigSyncState("dirty");
    } finally {
      btn.disabled = appState.tncState !== "connected";
    }
  };

  const configFormEl = document.getElementById("config-form");
  if (configFormEl) {
    configFormEl.addEventListener("submit", handleSaveConfig);
  } else {
    document.getElementById("btn-write-config").addEventListener("click", handleSaveConfig);
  }

  document.getElementById("btn-cmd-reset").addEventListener("click", async () => {
    const confirmed = await showConfirm(
      "Send reboot command (#RESET) to NET-51TNC? The device will restart and auto-reconnect.",
      "Reboot NET-51TNC"
    );
    if (!confirmed) return;
    try {
      const res = await fetch("/api/command/reset", { method: "POST" });
      if (res.ok) {
        updateTncConnectionState("rebooting", appState.ip, appState.port, 8);
        showToast("Reboot command sent to TNC (#RESET)", "warning");
      } else {
        const err = await res.json();
        showToast(`Reboot failed: ${err.detail || res.statusText}`, "error");
      }
    } catch (e) {
      showToast(`Reboot error: ${e.message}`, "error");
    }
  });

  document.getElementById("btn-cmd-install").addEventListener("click", async () => {
    const confirmed = await showConfirm(
      "WARNING: Restore NET-51TNC to factory default settings (#INSTALL)? All stored EEPROM parameters will be reset to factory defaults.",
      "⚠️ Factory Reset"
    );
    if (!confirmed) return;
    try {
      const res = await fetch("/api/command/install", { method: "POST" });
      if (res.ok) {
        updateTncConnectionState("rebooting", appState.ip, appState.port, 10);
        showToast("Factory reset command sent (#INSTALL)", "warning");
      } else {
        const err = await res.json();
        showToast(`Factory reset failed: ${err.detail || res.statusText}`, "error");
      }
    } catch (e) {
      showToast(`Factory reset error: ${e.message}`, "error");
    }
  });

  // Beacon Test Buttons
  const bindTestBtn = (id, endpoint, label) => {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener("click", async () => {
        try {
          const res = await fetch(endpoint, { method: "POST" });
          if (res.ok) {
            showToast(`${label} triggered`, "info");
          } else {
            const err = await res.json();
            showToast(`Failed to trigger ${label}: ${err.detail || res.statusText}`, "error");
          }
        } catch (e) {
          showToast(`Error triggering ${label}: ${e.message}`, "error");
        }
      });
    }
  };

  bindTestBtn("btn-test-gate-rf", "/api/command/gate_rf_test", "Gateway RF Beacon");
  bindTestBtn("btn-test-gate-is", "/api/command/gate_is_test", "Gateway IS Beacon");
  bindTestBtn("btn-test-wx-rf", "/api/command/wx_rf_test", "Weather RF Beacon");
  bindTestBtn("btn-test-wx-is", "/api/command/wx_is_test", "Weather IS Beacon");

  document.getElementById("btn-clear-stream").addEventListener("click", () => {
    document.getElementById("packet-console").innerHTML = "";
    showToast("Packet console cleared", "info");
  });

  document.getElementById("btn-clear-stations").addEventListener("click", async () => {
    const confirmed = await showConfirm("Clear all heard stations from memory?", "Clear Heard Stations");
    if (confirmed) {
      await fetch("/api/stations", { method: "DELETE" });
    }
  });

  const btnCalcPasscode = document.getElementById("btn-calc-passcode");
  if (btnCalcPasscode) {
    btnCalcPasscode.addEventListener("click", async () => {
      const call = getVal("cfg-callsign").trim().toUpperCase();
      if (!call) {
        showToast("Enter a callsign first to calculate APRS-IS passcode", "warning");
        return;
      }
      try {
        const res = await fetch(`/api/passcode/${encodeURIComponent(call)}`);
        if (res.ok) {
          const data = await res.json();
          setVal("cfg-passcode", data.passcode);
          checkConfigDirty();
          showToast(`Calculated APRS-IS passcode for ${data.callsign}: ${data.passcode}`, "success");
        } else {
          const err = await res.json();
          showToast(`Passcode error: ${err.detail || res.statusText}`, "error");
        }
      } catch (e) {
        showToast(`Passcode calculation failed: ${e.message}`, "error");
      }
    });
  }

  const btnExport = document.getElementById("btn-export-config");
  if (btnExport) {
    btnExport.addEventListener("click", () => {
      const cfg = gatherConfigForm();
      const call = cfg.callsign || "NET51TNC";
      const blob = new Blob([JSON.stringify(cfg, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${call}_web51tnc_config.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      showToast(`Exported configuration for ${call}`, "info");
    });
  }

  const btnImport = document.getElementById("btn-import-config");
  const fileImport = document.getElementById("file-import-config");
  if (btnImport && fileImport) {
    btnImport.addEventListener("click", () => fileImport.click());
    fileImport.addEventListener("change", async (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      try {
        const text = await file.text();
        const parsed = JSON.parse(text);
        const prevLoaded = appState.lastLoadedConfig;
        populateConfigForm(parsed);
        if (prevLoaded) {
          appState.lastLoadedConfig = prevLoaded;
          checkConfigDirty();
        } else {
          setConfigSyncState("dirty");
        }
        showToast(`Imported configuration from ${file.name}`, "success");
      } catch (err) {
        showToast(`Invalid JSON configuration file: ${err.message}`, "error");
      } finally {
        fileImport.value = "";
      }
    });
  }

  const btnLoadInitial = document.getElementById("btn-load-initial");
  if (btnLoadInitial) {
    btnLoadInitial.addEventListener("click", async () => {
      try {
        const res = await fetch("/api/eeprom/initial");
        if (res.ok) {
          const initialCfg = await res.json();
          const prevLoaded = appState.lastLoadedConfig;
          populateConfigForm(initialCfg);
          if (prevLoaded) {
            appState.lastLoadedConfig = prevLoaded;
            checkConfigDirty();
          } else {
            setConfigSyncState("dirty");
          }
          showToast("Loaded safekept first EEPROM download into form (click Save to write to TNC)", "success");
        } else {
          const err = await res.json();
          showToast(`No initial backup available: ${err.detail || res.statusText}`, "warning");
        }
      } catch (e) {
        showToast(`Failed to load initial EEPROM backup: ${e.message}`, "error");
      }
    });
  }

  document.getElementById("station-filter").addEventListener("input", renderStationsTable);
  const chkRfOnly = document.getElementById("chk-rf-only");
  if (chkRfOnly) {
    chkRfOnly.addEventListener("change", renderStationsTable);
  }
  const freshnessSelect = document.getElementById("station-freshness-filter");
  if (freshnessSelect) {
    freshnessSelect.addEventListener("change", renderStationsTable);
  }

  const btnCenterHome = document.getElementById("btn-center-home");
  if (btnCenterHome) {
    btnCenterHome.addEventListener("click", () => {
      if (!map) return;
      if (homeCoords) {
        map.setView(homeCoords, 12);
        if (homeMarker) homeMarker.openPopup();
      } else {
        showToast("No Gateway coordinates configured in TNC yet", "warning");
      }
    });
  }

  document.getElementById("btn-fit-map").addEventListener("click", () => {
    if (!map) return;
    const latlngs = Object.values(markers).map(m => m.getLatLng());
    if (homeMarker) {
      latlngs.push(homeMarker.getLatLng());
    }
    if (latlngs.length > 0) {
      map.fitBounds(L.latLngBounds(latlngs), { padding: [40, 40], maxZoom: 13 });
    }
  });

  const chkShowTracks = document.getElementById("chk-show-tracks");
  if (chkShowTracks) {
    chkShowTracks.addEventListener("change", () => {
      if (!map) return;
      const show = chkShowTracks.checked;
      Object.values(polylines).forEach((poly) => {
        if (show && !map.hasLayer(poly)) {
          poly.addTo(map);
        } else if (!show && map.hasLayer(poly)) {
          map.removeLayer(poly);
        }
      });
    });
  }
}

function escapeHtml(text) {
  if (text === null || text === undefined) return "";
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// ==============================================================================
// Quick Station & RPi Wi-Fi / Fallback AP Provisioning Wizard
// ==============================================================================

function computeAprsPasscodeLocal(callsign) {
  const base = (callsign || "").trim().toUpperCase().split("-")[0];
  if (!base || !/^[A-Z0-9]{1,6}$/.test(base)) return "";
  let hash = 0x73e2;
  for (let i = 0; i < base.length; i += 2) {
    hash ^= base.charCodeAt(i) << 8;
    if (i + 1 < base.length) {
      hash ^= base.charCodeAt(i + 1);
    }
  }
  return String(hash & 0x7fff);
}

let qsSelectedTable = "/";
let qsSelectedCode = "r";
let qsAutoOpenedOnce = false;

function renderQuickSetupIconPicker() {
  const container = document.getElementById("qs-icon-picker");
  if (!container) return;
  container.innerHTML = "";

  APRS_SYMBOL_CATALOG.forEach((item) => {
    const combo = `${item.table}${item.code}`;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "aprs-icon-tile";
    btn.dataset.table = item.table;
    btn.dataset.code = item.code;
    btn.dataset.combo = combo;
    btn.title = `${item.name} (${combo})`;
    btn.innerHTML = `
      <span class="aprs-icon-glyph">${item.icon}</span>
      <span class="aprs-icon-name">${escapeHtml(item.short)}</span>
      <span class="aprs-icon-code">${escapeHtml(combo)}</span>
    `;
    btn.addEventListener("click", () => {
      qsSelectedTable = item.table;
      qsSelectedCode = item.code;
      syncQuickSetupSelectionUI();
    });
    container.appendChild(btn);
  });
  syncQuickSetupSelectionUI();
}

function syncQuickSetupSelectionUI() {
  const call = (getVal("qs-callsign", "NOCALL").trim().toUpperCase()) || "NOCALL";
  const ssid = intOrDefault(getVal("qs-ssid", "7"), 7);
  const fullCall = ssid > 0 ? `${call}-${ssid}` : call;
  const meta = getAprsSymbolMeta(qsSelectedTable, qsSelectedCode);

  const previewEl = document.getElementById("qs-fullcall-preview");
  if (previewEl) {
    previewEl.textContent = `${meta.icon} ${fullCall}`;
  }

  const pills = document.querySelectorAll("#qs-ssid-pills .ssid-pill");
  pills.forEach((pill) => {
    pill.classList.toggle("active", Number(pill.dataset.ssid) === ssid);
  });

  const combo = `${qsSelectedTable}${qsSelectedCode}`;
  const container = document.getElementById("qs-icon-picker");
  if (container) {
    container.querySelectorAll(".aprs-icon-tile").forEach((tile) => {
      tile.classList.toggle("active", tile.dataset.combo === combo);
    });
  }
}

function syncQuickSetupFromConfig(cfg) {
  if (!cfg) return;
  const call = (cfg.callsign || "").trim().toUpperCase();
  const ssid = cfg.gateway_ssid !== undefined ? cfg.gateway_ssid : (cfg.ssid !== undefined ? cfg.ssid : 7);
  setVal("qs-callsign", call);
  setVal("qs-ssid", ssid);
  const pass = cfg.passcode || computeAprsPasscodeLocal(call);
  setVal("qs-passcode", pass);

  qsSelectedTable = cfg.gateway_symbol_table || "/";
  qsSelectedCode = cfg.gateway_symbol_code || "r";

  const decLat = aprsToDecimal(cfg.gateway_lat, false);
  const decLon = aprsToDecimal(cfg.gateway_lon, true);
  if (decLat !== null) setVal("qs-lat", decLat);
  if (decLon !== null) setVal("qs-lon", decLon);

  syncQuickSetupSelectionUI();
}

async function refreshWifiStatus(autoOpenIfNeeded = false) {
  const box = document.getElementById("qs-wifi-status-box");
  try {
    const res = await fetch("/api/wifi/status");
    if (!res.ok) return;
    const data = await res.json();
    if (box) {
      if (data.mode === "ap") {
        box.innerHTML = `🟠 <b>Hotspot AP Mode Active:</b> SSID <code>${escapeHtml(data.ap_ssid)}</code> — Gateway IP <code>${escapeHtml(data.ap_ip || "10.42.0.1")}</code><br/><span class="text-muted">Select your local Wi-Fi below to connect this Raspberry Pi to the internet.</span>`;
      } else if (data.mode === "client") {
        box.innerHTML = `🟢 <b>Connected to Wi-Fi:</b> <code>${escapeHtml(data.ssid)}</code> — RPi IP <code>${escapeHtml(data.ip || "DHCP")}</code>`;
        if (!getVal("qs-wifi-ssid")) {
          setVal("qs-wifi-ssid", data.ssid);
        }
      } else {
        box.innerHTML = `⚪ <b>Wi-Fi Disconnected</b> — Scan and select a network below or open Hotspot AP.`;
      }
    }
    if (autoOpenIfNeeded && !qsAutoOpenedOnce && data.needs_setup) {
      qsAutoOpenedOnce = true;
      openQuickSetupModal();
    }
  } catch (e) {
    if (box) {
      box.textContent = "Wi-Fi status unavailable.";
    }
  }
}

async function scanWifiNetworks() {
  const selectEl = document.getElementById("qs-wifi-list");
  const btnScan = document.getElementById("btn-qs-wifi-scan");
  if (!selectEl) return;
  if (btnScan) {
    btnScan.disabled = true;
    btnScan.textContent = "⏳ Scanning...";
  }
  try {
    const res = await fetch("/api/wifi/scan");
    if (res.ok) {
      const data = await res.json();
      const nets = data.networks || [];
      selectEl.innerHTML = '<option value="">-- Select a scanned Wi-Fi network --</option>';
      nets.forEach((n) => {
        const opt = document.createElement("option");
        opt.value = n.ssid;
        opt.textContent = `${n.active ? "🟢 " : "📶 "}${n.ssid} (${n.signal}% — ${n.security})`;
        selectEl.appendChild(opt);
      });
      showToast(`Found ${nets.length} Wi-Fi network(s)`, "info");
    }
  } catch (e) {
    showToast(`Wi-Fi scan error: ${e.message}`, "error");
  } finally {
    if (btnScan) {
      btnScan.disabled = false;
      btnScan.textContent = "🔄 Scan";
    }
  }
}

function openQuickSetupModal() {
  const modal = document.getElementById("quick-setup-modal");
  if (!modal) return;
  if (appState.lastLoadedConfig) {
    syncQuickSetupFromConfig(appState.lastLoadedConfig);
  } else {
    syncQuickSetupFromConfig(gatherConfigForm());
  }
  modal.style.display = "flex";
  refreshWifiStatus(false);
}

function initQuickSetupWizard() {
  renderQuickSetupIconPicker();

  const btnOpen = document.getElementById("btn-quick-setup");
  const btnClose = document.getElementById("btn-qs-close");
  const modal = document.getElementById("quick-setup-modal");

  if (btnOpen) {
    btnOpen.addEventListener("click", () => {
      openQuickSetupModal();
      scanWifiNetworks();
    });
  }
  if (btnClose && modal) {
    btnClose.addEventListener("click", () => {
      modal.style.display = "none";
    });
  }

  // Also auto-calculate passcode in main config form when typing callsign
  const cfgCallInput = document.getElementById("cfg-callsign");
  if (cfgCallInput) {
    cfgCallInput.addEventListener("input", () => {
      const autoPass = computeAprsPasscodeLocal(cfgCallInput.value);
      if (autoPass) {
        setVal("cfg-passcode", autoPass);
      }
    });
  }

  // Auto-calculate passcode in Quick Setup Wizard as user types callsign
  const qsCallInput = document.getElementById("qs-callsign");
  if (qsCallInput) {
    qsCallInput.addEventListener("input", () => {
      const clean = qsCallInput.value.toUpperCase().replace(/[^A-Z0-9]/g, "");
      if (qsCallInput.value !== clean) qsCallInput.value = clean;
      const autoPass = computeAprsPasscodeLocal(clean);
      if (autoPass) {
        setVal("qs-passcode", autoPass);
      }
      syncQuickSetupSelectionUI();
    });
  }

  const qsSsidInput = document.getElementById("qs-ssid");
  if (qsSsidInput) {
    qsSsidInput.addEventListener("input", () => {
      syncQuickSetupSelectionUI();
    });
  }

  // SSID Role Preset Pills (-7, -9, -10, -13, -5, 0)
  document.querySelectorAll("#qs-ssid-pills .ssid-pill").forEach((pill) => {
    pill.addEventListener("click", () => {
      setVal("qs-ssid", pill.dataset.ssid);
      if (pill.dataset.table && pill.dataset.code) {
        qsSelectedTable = pill.dataset.table;
        qsSelectedCode = pill.dataset.code;
      }
      syncQuickSetupSelectionUI();
    });
  });

  // Pick on Map from Quick Setup Wizard
  const btnPickMap = document.getElementById("btn-qs-pick-map");
  if (btnPickMap && modal) {
    btnPickMap.addEventListener("click", () => {
      modal.style.display = "none";
      appState.pickingFromWizard = true;
      startMapCoordPicker();
    });
  }

  // Browser GPS in Quick Setup Wizard
  const btnQsGps = document.getElementById("btn-qs-gps");
  if (btnQsGps) {
    btnQsGps.addEventListener("click", () => {
      if (!navigator.geolocation) {
        showToast("Geolocation is not supported by this browser", "warning");
        return;
      }
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          setVal("qs-lat", pos.coords.latitude.toFixed(5));
          setVal("qs-lon", pos.coords.longitude.toFixed(5));
          showToast("Coordinates updated from browser GPS", "success");
        },
        (err) => {
          showToast(`GPS unavailable (${err.message}) — use Pick on Map instead`, "warning");
        },
        { timeout: 8000 }
      );
    });
  }

  // Save Station Identity, SSID & Icon directly to NET-51TNC EEPROM
  const btnSaveTnc = document.getElementById("btn-qs-save-tnc");
  if (btnSaveTnc) {
    btnSaveTnc.addEventListener("click", async () => {
      const call = getVal("qs-callsign").trim().toUpperCase();
      if (!call || !/^[A-Z0-9]{1,6}$/.test(call)) {
        showToast("Please enter a valid amateur callsign (1–6 characters, no SSID)", "warning");
        return;
      }
      const ssid = Math.min(15, Math.max(0, intOrDefault(getVal("qs-ssid", "7"), 7)));
      const passcode = getVal("qs-passcode").trim() || computeAprsPasscodeLocal(call);

      setVal("cfg-callsign", call);
      setVal("cfg-ssid", ssid);
      setVal("cfg-gw-ssid", ssid);
      setVal("cfg-passcode", passcode);
      setVal("cfg-gw-sym-table", qsSelectedTable);
      setVal("cfg-gw-sym-code", qsSelectedCode);

      const decLat = parseFloat(getVal("qs-lat"));
      const decLon = parseFloat(getVal("qs-lon"));
      if (!isNaN(decLat) && !isNaN(decLon)) {
        const aprsLat = decimalToAprs(decLat, false);
        const aprsLon = decimalToAprs(decLon, true);
        if (aprsLat && aprsLon) {
          setVal("cfg-gw-lat", aprsLat);
          setVal("cfg-gw-lon", aprsLon);
          setVal("calc-dec-lat", decLat.toFixed(5));
          setVal("calc-dec-lon", decLon.toFixed(5));
        }
      }

      syncGatewaySymbolPreset();
      updateGatewayBeaconPreview();

      btnSaveTnc.disabled = true;
      btnSaveTnc.textContent = "⏳ Writing to TNC EEPROM...";
      try {
        const cfg = gatherConfigForm();
        const res = await fetch("/api/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(cfg),
        });
        if (res.ok) {
          const resJson = await res.json();
          if (resJson.config) {
            populateConfigForm(resJson.config);
          }
          showToast(`Saved ${call}-${ssid} to NET-51TNC EEPROM! Hardware is restarting.`, "success", 5000);
          updateTncConnectionState("rebooting", appState.ip, appState.port, resJson.reboot_seconds || 8);
          if (modal) modal.style.display = "none";
        } else {
          const err = await res.json();
          showToast(`Failed to write TNC EEPROM: ${err.detail || res.statusText}`, "error");
        }
      } catch (e) {
        showToast(`Save error: ${e.message}`, "error");
      } finally {
        btnSaveTnc.disabled = false;
        btnSaveTnc.textContent = "💾 Save Callsign, SSID & Icon to TNC";
      }
    });
  }

  // Wi-Fi Scan & Connect controls
  const btnWifiScan = document.getElementById("btn-qs-wifi-scan");
  if (btnWifiScan) {
    btnWifiScan.addEventListener("click", scanWifiNetworks);
  }

  const wifiList = document.getElementById("qs-wifi-list");
  if (wifiList) {
    wifiList.addEventListener("change", () => {
      if (wifiList.value) {
        setVal("qs-wifi-ssid", wifiList.value);
      }
    });
  }

  const btnWifiConnect = document.getElementById("btn-qs-wifi-connect");
  if (btnWifiConnect) {
    btnWifiConnect.addEventListener("click", async () => {
      const ssid = getVal("qs-wifi-ssid").trim();
      const password = document.getElementById("qs-wifi-psk")?.value || "";
      if (!ssid) {
        showToast("Please select or enter a Wi-Fi SSID", "warning");
        return;
      }
      const confirmed = await showConfirm(
        `Connect Raspberry Pi to Wi-Fi network '${ssid}'? If the RPi changes networks, its IP address may change (reachable at http://web51tnc.local:8000).`,
        "Switch Raspberry Pi Wi-Fi"
      );
      if (!confirmed) return;

      btnWifiConnect.disabled = true;
      btnWifiConnect.textContent = "⏳ Connecting Wi-Fi...";
      try {
        const res = await fetch("/api/wifi/connect", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ssid, password }),
        });
        if (res.ok) {
          const data = await res.json();
          showToast(`Connected to Wi-Fi '${ssid}' (IP: ${data.wifi?.ip || "assigned"})`, "success", 6000);
          await refreshWifiStatus(false);
        } else {
          const err = await res.json();
          showToast(`Wi-Fi connection failed: ${err.detail || res.statusText}`, "error", 6000);
          await refreshWifiStatus(false);
        }
      } catch (e) {
        showToast("Wi-Fi link switching — reconnect to the new Wi-Fi and open http://web51tnc.local:8000", "info", 8000);
      } finally {
        btnWifiConnect.disabled = false;
        btnWifiConnect.textContent = "📶 Connect RPi to This Wi-Fi";
      }
    });
  }

  const btnWifiAp = document.getElementById("btn-qs-wifi-ap");
  if (btnWifiAp) {
    btnWifiAp.addEventListener("click", async () => {
      const confirmed = await showConfirm(
        "Switch Raspberry Pi into Hotspot Access Point mode (SSID: Web51TNC-Setup, password: web51tnc, URL: http://10.42.0.1:8000)? This will disconnect the current Wi-Fi client session until you connect a new Wi-Fi.",
        "Start Hotspot AP Mode"
      );
      if (!confirmed) return;
      try {
        await fetch("/api/wifi/ap", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "start" }),
        });
        showToast("Hotspot AP 'Web51TNC-Setup' starting at http://10.42.0.1:8000", "warning", 8000);
      } catch (e) {
        showToast("Hotspot AP 'Web51TNC-Setup' starting — connect to Wi-Fi 'Web51TNC-Setup' (password: web51tnc)", "info", 8000);
      }
    });
  }

  // Check Wi-Fi / setup state shortly after boot
  setTimeout(() => refreshWifiStatus(true), 1200);
}
