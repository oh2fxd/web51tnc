# NET-51TNC / APRS 51TNC Technical Protocol Specification

This document details the reverse-engineered network protocol, control command set, and internal 512-byte EEPROM memory layout of the **NET-51TNC** (and APRS 51TNC/51TNC2) hardware TNC / iGate / digipeater device developed by BH4TDV.

All details were verified against the official Windows configuration utility (`APRS 51TNC SETUP _ENGLISH_20140701.exe`) via deep CIL disassembly and runtime binary inspection.

---

## 1. Transport & Network Layer

* **Protocol:** Raw TCP stream socket
* **Default IP Address:** `192.168.1.120` (factory default static IP, Gateway `192.168.1.1`, Mask `255.255.255.0`)
* **Configuration & Live Data Port:** `60000` (TCP)
* **APRS-IS Uplink Local Sockets:** Dynamic/static UDP ports `5000`–`5005` (used by TNC when connecting to APRS-IS servers)
* **Connection Lifecycle:** 
  * The TNC listens continuously on TCP port 60000.
  * Once a client connects, the TNC maintains the socket open.
  * The same port is used for reading/writing configuration EEPROM and streaming live incoming/outgoing APRS packets, server connection updates, and weather telemetry.

### Hardware Pinout & Recovery Notes (from BH4TDV Schematics)
* **Emergency Hardware Reset**:
  * Power off the TNC.
  * Short pins **IO3** and **IO4** together on the expansion header.
  * Power on with pins shorted; both **RXD** and **TXD** LEDs will illuminate solid, indicating reset complete.
  * Power off, remove short, and power on. IP is reset to `192.168.1.120`, Gateway `192.168.1.1`.
* **Hardware Squelch (SQL) Input**:
  * Microcontroller: STC 8051 (e.g. STC12C5A60S2) with Port 4.
  * CPU Pin 29 (P4.6) is connected via a `1N4148` diode (anode to CPU, cathode to radio DATA jack SQL line) for carrier detect / squelch lock.
* **Audio Sensitivity Mod (R36)**:
  * If low-level HTs (e.g. Yaesu FT-8GR, certain Baofengs) fail to decode due to low audio deviation, replace resistor **R36** on the TNC board with **10kΩ**.
* **Expansion Headers**:
  * **IO1**: GPS or Weather Interface Board (TTL 4800 / 9600 bps).
  * **IO2**: Secondary `DS18B20` temperature sensor (or Weather board DAT line).
  * **IO3**: LCD(R/W).
  * **IO4**: LCD(E).
* **Onboard Buzzer Signals**:
  * `1 short beep`: Valid packet decoded from RF.
  * `3 short beeps`: Incoming APRS text message received over APRS-IS directed to the gateway callsign.

### GPS Debug & Status Codes
When GPS is enabled, the TNC outputs the following diagnostic codes over the control stream:
* `E0`: No GPS connected (`GPS NO LINK`)
* `E1`: Baud rate mismatch or synchronization loss
* `E2`: Frame format error (first character after frame pause is not `$`)
* `E3`: Unexpected frame termination or mid-stream disconnect
* `E4`: Complete NMEA frame received, but missing `$GPRMC` sentence
* `E5`: GPS receiver unlocked / searching for satellites (`GPS NOLOCK`)
* `E8`: GPS locked with valid fix; timer expired or SmartBeacon criteria met (transmitted OK)
* `E9`: GPS locked with valid fix; waiting for next interval / SmartBeacon condition

---

## 2. Command Set

Commands are sent as raw ASCII strings over TCP port 60000 without trailing delimiters unless specified:

| Command ASCII | Bytes | Description | TNC Response / Behavior |
| :--- | :--- | :--- | :--- |
| `#HELLO` | 6 | Request full EEPROM configuration | Returns 518 bytes (`#HELLO` + 512 raw EEPROM bytes). Following bytes contain status text. |
| `#HELLO` + 512 bytes | 518 | Write 512 bytes configuration to EEPROM | TNC writes to non-volatile flash and reboots |
| `#RESET` | 6 | Soft reboot | TNC reboots its MCU and network core |
| `#INSTALL` | 8 | Factory reset | Restores default factory parameters and reboots |
| `#GATE_RF_TEST` | 13 | Gateway RF Beacon Test | Transmits gateway APRS beacon packet over radio (RF) |
| `#GATE_IS_TEST` | 13 | Gateway APRS-IS Beacon Test | Sends gateway APRS beacon packet to APRS-IS server |
| `#WX_RF_TEST` | 11 | Weather RF Beacon Test | Transmits weather telemetry packet over radio (RF) |
| `#WX_IS_TEST` | 11 | Weather APRS-IS Beacon Test | Sends weather telemetry packet to APRS-IS server |

---

## 3. 512-Byte EEPROM Memory Map

When `#HELLO` is received, the TNC replies with 518 bytes:
* Bytes 0–5: ASCII `#HELLO` header (`0x23, 0x48, 0x45, 0x4C, 0x4C, 0x4F`)
* Bytes 6–517: 512 bytes of raw EEPROM configuration data

The layout of the 512 EEPROM bytes (index 0 to 511 relative to EEPROM payload start) is verified below:

### Station Identity (0–15)
| Offset | Size | Type | Field | Description |
| :--- | :--- | :--- | :--- | :--- |
| `0x000` (0) | 6 | ASCII | `callsign` | Callsign (uppercase, max 6 chars, null-padded) |
| `0x007` (7) | 1 | uint8 | `ssid` | Callsign SSID (0–15) |
| `0x009` (9) | 6 | ASCII | `passcode` | APRS-IS login passcode (e.g. `"12345"` or `"-1"`) |

### Ethernet & Network Configuration (16–33)
| Offset | Size | Type | Field | Description |
| :--- | :--- | :--- | :--- | :--- |
| `0x010` (16) | 4 | uint8[4] | `ip_address` | Static IP (e.g., `192.168.1.120`) |
| `0x014` (20) | 4 | uint8[4] | `netmask` | Subnet mask (e.g., `255.255.255.0`) |
| `0x018` (24) | 4 | uint8[4] | `gateway` | Default gateway IP (e.g., `192.168.1.1`) |
| `0x01C` (28) | 6 | uint8[6] | `mac_address` | Hardware MAC address (e.g., `00:08:DC:11:22:33`) |

### APRS-IS Servers & Filtering (34–127)
| Offset | Size | Type | Field | Description |
| :--- | :--- | :--- | :--- | :--- |
| `0x022` (34) | 4 | uint8[4] | `server_a` | Primary APRS-IS server IP |
| `0x026` (38) | 4 | uint8[4] | `server_b` | Secondary APRS-IS server IP |
| `0x02A` (42) | 4 | uint8[4] | `server_c` | Tertiary APRS-IS server IP |
| `0x02E` (46) | 1 | uint8 | `allow_is_to_rf`| Allow APRS-IS to RF forwarding (`0`=No, `1`=Yes) |
| `0x02F` (47) | 1 | uint8 | `server_mode` | `0` = Automatic rotation, `1` = Always Server A |
| `0x030` (48) | 1 | uint8 | `timeout_restart`| Auto restart connection on timeout (`0`=No, `1`=Yes) |
| `0x031` (49) | 1 | uint8 | `hellocq_enabled`| Connect to default APRS.HELLOCQ.NET (`0`=No, `1`=Yes) |
| `0x033` (51) | 1 | uint8 | `lcd_type` | `0` = Off/Close, `1` = ST7920 12864, `2` = Color 320x240 |
| `0x034` (52) | 1 | uint8 | `pm25_enabled` | Enable PM 2.5 air quality sensor detection (`0`/`1`) |
| `0x040` (64) | 64 | ASCII | `aprs_filter` | APRS-IS filter definition string (e.g. `m/50`) |

### Gateway APRS Beacon (128–255)
| Offset | Size | Type | Field | Description |
| :--- | :--- | :--- | :--- | :--- |
| `0x080` (128) | 2 | uint16 (BE) | `gateway_beacon_is_sec` | Beacon interval over APRS-IS in seconds (`0`=Off) |
| `0x082` (130) | 2 | uint16 (BE) | `gateway_beacon_rf_sec` | Beacon interval over RF in seconds (`0`=Off) |
| `0x084` (132) | 1 | uint8 | `gateway_path_idx` | Path index (`0`=`- - -`, `1`=`WIDE1-1`, `2`=`WIDE1-1,WIDE2-1`) |
| `0x085` (133) | 1 | uint8 | `gateway_report_voltage`| Report motherboard voltage in beacon (`0`/`1`) |
| `0x087` (135) | 1 | uint8 | `gateway_ssid` | Gateway beacon SSID (0–15) |
| `0x090` (144) | 1 | char | `gateway_packet_type` | APRS packet data type identifier (`!` or `=`) |
| `0x091` (145) | 8 | ASCII | `gateway_lat` | Latitude in APRS format (e.g. `6010.20N`) |
| `0x099` (153) | 1 | char | `gateway_symbol_table` | APRS symbol table / overlay (`/`, `\`, `0-9`, `A-Z`) |
| `0x09A` (154) | 9 | ASCII | `gateway_lon` | Longitude in APRS format (e.g. `02456.30E`) |
| `0x0A3` (163) | 1 | char | `gateway_symbol_code` | APRS symbol code character (e.g. `r` Repeater, `&` Gateway) |
| `0x0A4` (164) | 70 | ASCII | `gateway_comment` | Custom beacon comment / status message |

### Weather Beacon (256–383)
| Offset | Size | Type | Field | Description |
| :--- | :--- | :--- | :--- | :--- |
| `0x100` (256) | 2 | uint16 (BE) | `weather_beacon_is_sec` | Weather beacon interval over APRS-IS in seconds |
| `0x102` (258) | 2 | uint16 (BE) | `weather_beacon_rf_sec` | Weather beacon interval over RF in seconds |
| `0x104` (260) | 1 | uint8 | `weather_path_idx` | Weather path index (`0`=`- - -`, `1`=`WIDE1-1`, `2`=`WIDE1-1,WIDE2-1`) |
| `0x105` (261) | 1 | uint8 | `weather_report_voltage`| Report supply voltage (`0`/`1`) |
| `0x107` (263) | 1 | uint8 | `weather_ssid` | Weather station SSID (0–15) |
| `0x108` (264) | 1 | uint8 | `weather_use_gps_coords`| Use GPS (IO1) coordinates for weather beacon (`0`/`1`) |
| `0x10B` (267) | 1 | uint8 | `weather_lcd_display` | LCD display weather data (`0`/`1`) |
| `0x110` (272) | 1 | char | `weather_packet_type` | APRS packet data type identifier (`=` or `!`) |
| `0x111` (273) | 8 | ASCII | `weather_lat` | Weather station latitude |
| `0x119` (281) | 1 | char | `weather_symbol_table` | Symbol table / overlay (`/` or `\`) |
| `0x11A` (282) | 9 | ASCII | `weather_lon` | Weather station longitude |
| `0x123` (291) | 1 | char | `weather_symbol_code` | Symbol code (`_` for WX Station) |
| `0x124` (292) | 33 | ASCII | `weather_telemetry_format` | Weather telemetry template (`c000s000g000t050r000p000h50b10050`) |
| `0x145` (325) | 50 | ASCII | `weather_comment` | Weather station comment / telemetry info |

### Radio Modem & Digipeater (384–395)
| Offset | Size | Type | Field | Description |
| :--- | :--- | :--- | :--- | :--- |
| `0x180` (384) | 1 | uint8 | `ptt_delay` | `0`=250ms, `1`=500ms, `2`=750ms, `3`=1000ms, `4`=1250ms, `5`=1500ms |
| `0x181` (385) | 1 | uint8 | `min_tx_interval` | `0`=250ms, `1`=500ms, `2`=750ms, `3`=1000ms |
| `0x183` (387) | 1 | uint8 | `tx_level` | `0` = YAESU Low (standard line level), `1` = ICOM High |
| `0x184` (388) | 1 | uint8 | `baud_mode` | `0`=Clever 4800, `1`=Text 9600, `2`=KISS 9600, `3`=Clever 9600 |
| `0x185` (389) | 1 | uint8 | `digi_wide1` | Enable WIDE1-1 digipeater (`0`=Off, `1`=On) |
| `0x186` (390) | 1 | uint8 | `digi_wide2` | Enable WIDE2 digipeating (`0`=Off, `1`=On) |
| `0x187` (391) | 1 | uint8 | `insert_gw_call` | Insert gateway callsign into header (`0`=Off, `1`=On) |
| `0x188` (392) | 1 | uint8 | `tx_waiting` | Waiting time code (`0`=250ms, `1`=500ms, `2`=750ms, `3`=1000ms) |
| `0x189` (393) | 1 | uint8 | `wait_channel_idle`| Wait for quiet channel before TX (`0`=Off, `1`=On) |
| `0x18A` (394) | 1 | uint8 | `sql_detect` | Squelch hardware detection (`0`=Off, `1`=On) |
| `0x18B` (395) | 1 | uint8 | `rf_lcd_display` | RF LCD simultaneous display decoded data (`0`/`1`) |

### GPS & Tracking Options (396–492)
| Offset | Size | Type | Field | Description |
| :--- | :--- | :--- | :--- | :--- |
| `0x18C` (396) | 1 | uint8 | `gps_baud` | GPS IO1 port baud rate (`0`=4800, `1`=9600) |
| `0x18D` (397) | 1 | uint8 | `gps_lcd_display` | LCD simultaneous display of GPS data (`0`/`1`) |
| `0x18E` (398) | 1 | uint8 | `gps_tracks` | Report heading, speed, altitude (`0`/`1`) |
| `0x190` (400) | 2 | uint16 (BE) | `gps_interval` | GPS beacon interval in seconds (0–65535) |
| `0x192` (402) | 1 | uint8 | `gps_mode` | `0`=Close, `1`=Timing, `2`=SmartBeacon, `3`=Manual/IO2 |
| `0x193` (403) | 1 | uint8 | `gps_ssid` | GPS beacon SSID (0–15) |
| `0x194` (404) | 1 | uint8 | `gps_path_idx` | Forwarding path index (`0`=`- - -`, `1`=`WIDE1-1`, `2`=`WIDE1-1,WIDE2-1`) |
| `0x195` (405) | 1 | char | `gps_sym_table` | GPS beacon symbol table |
| `0x196` (406) | 1 | char | `gps_sym_code` | GPS beacon symbol code |
| `0x197` (407) | 1 | char | `gps_sym_overlay` | Symbol overlay character |
| `0x198` (408) | 1 | uint8 | `report_voltage` | Transmit supply voltage in comment (`0`/`1`) |
| `0x199` (409) | 1 | uint8 | `gps_offset_en` | Coordinate offset enable (`0`/`1`) |
| `0x19A` (410) | 1 | uint8 | `lat_offset_neg`| Latitude offset sign (`0` = `+`, `1` = `-`) |
| `0x19B` (411) | 1 | uint8 | `lat_offset` | Latitude offset value (0.01 deg units) |
| `0x19C` (412) | 1 | uint8 | `lon_offset_neg`| Longitude offset sign (`0` = `+`, `1` = `-`) |
| `0x19D` (413) | 1 | uint8 | `lon_offset` | Longitude offset value (0.01 deg units) |
| `0x1B0` (432) | 60 | ASCII | `gps_custom_info`| Custom tracking information string |

---

## 4. Live Stream & Packet Framing

While connected to TCP port 60000, incoming data arrives as CRLF (`\r\n`) delimited lines:

1. **RF Decoded Packet:**
   ```text
   RF ->TNC : N0CALL-9>APRS,WIDE1-1:=6010.50N/02456.20E#Portable Station
   ```
   Standard AX.25 APRS frame emitted when decoded over the radio receiver (prefixed with `RF ->TNC :` on firmware v2016/2017+).

2. **Gateway / APRS-IS Packet:**
   ```text
   GATE ->IS: N0CALL-10>APET51,WIDE1-1:!6010.20N/02456.30Er
   # aprsc 3.0.1-g79fe075 15 Nov 2026 03:58:23 GMT T2PRT 193.137.237.12:14580
   ```
   Lines prefixed with `GATE ->IS:`, `TNC ->IS :`, or `# ` denote local gateway beacons, iGated packets, or APRS-IS server traffic. (Note: `TTL->GARMIN: $GPWPL,...` waypoint output lines are automatically filtered out from APRS station parsing.)

3. **Status / Telemetry Line:**
   Periodic status updates containing supply voltage, server connection status, or external weather sensor readings.
   The weather telemetry string follows the standard format:
   ```text
   c000s000g000t050r000p000h50b10050
   ```
   * `c000`: Wind direction in degrees (`000`–`360`)
   * `s000`: Wind speed (1-minute sustained) in mph
   * `g000`: Wind gust (5-minute peak) in mph
   * `t050`: Temperature in Fahrenheit (`050` = 50°F / 10°C)
   * `r000`: Rainfall in last 1 hour (hundredths of an inch)
   * `p000`: Rainfall in last 24 hours (hundredths of an inch)
   * `h50`: Relative humidity in percent (`50` = 50%)
   * `b10050`: Barometric pressure in tenths of hPa (`10050` = 1005.0 hPa)

---

## 5. Firmware Release History & Compatibility

Extracted from BH4TDV internal release notes and official repository (`BH4TDV/APRS-51TNC`):

| Release Date | Major Changes & Notes | Compatibility |
| :--- | :--- | :--- |
| **2012-12-28** | Added RF packet length overflow protection; fixed overlapping packet corruption. | Initial public release |
| **2013-01-14** | Added APRS-IS uplink socket support (Hellocq / APRS2 servers). | Network iGate added |
| **2013-02-04** | Added GPS NMEA 4800/9600 auto-baud support; added SmartBeacon logic. | GPS tracking added |
| **2013-08-05** | Migrated weather sensor handling to dedicated external interface board. | Format updated |
| **2014-01-16** | Added manual hardware reset button / jumper. Default IP `192.168.1.120`. | HW reset added |
| **2014-02-28** | Hardware reset procedure finalized: Short IO3 + IO4 on boot. | Standard recovery |
| **2014-03-07** | Gateway beacon added PM2.5 particulate sensor data (`ug/m³`). | PM2.5 support |
| **2014-07-01** | English setup client release (`APRS 51TNC SETUP _ENGLISH_20140701.exe`). | Web51TNC baseline |
| **2017-07-09** | Weather beacon added PM2.5 support; auto-suppresses comment when active. | 100% Compatible |
| **2017-12-27** | Fixed WiFi module serial data output formatting (`APRS_51TNC_20171227.exe`). | 100% Compatible |

> [!NOTE]
> Deep reverse-engineering of both `20140701` and `20171227` clients confirms that the core **512-byte EEPROM layout, TCP control port (60000), and command set (`#HELLO`, `#RESET`, `#INSTALL`) remained 100% backward- and forward-compatible**. Web51TNC supports all firmware versions seamlessly.

