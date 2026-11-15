# NET-51TNC Hardware Connection & Wiring Guide

This guide covers interfacing the **NET-51TNC** with a handheld transceiver (e.g. Wouxun KG-UV8D / KG-UV9D / Baofeng / Kenwood) and directly connecting to a **Raspberry Pi** via Ethernet.

---

## 1. Raspberry Pi Direct Ethernet Connection

No router, switch, or crossover cable is necessary. All Raspberry Pi models (3B, 3B+, 4B, 5) support hardware **Auto-MDIX**, allowing a direct straight-through RJ45 cable to negotiate link automatically.

```
+--------------------+  Ethernet Patch Cable  +--------------------+
|  Raspberry Pi 4/5  |<======================>|     NET-51TNC      |
|  eth0: 192.168.1.50|                        |  IP: 192.168.1.120 |
+--------------------+                        +--------------------+
```

### Raspberry Pi Network Configuration

The NET51TNC comes preconfigured with static IP **`192.168.1.120`**. Configure your Pi's `eth0` to be in the same subnet:

#### Method A: Modern Raspberry Pi OS (Bookworm / NetworkManager)
```bash
# Set static IP on wired Ethernet connection
sudo nmcli connection modify "Wired connection 1" \
  ipv4.addresses 192.168.1.50/24 \
  ipv4.method manual

sudo nmcli connection up "Wired connection 1"
```

#### Method B: Quick Temporary Test
```bash
sudo ip addr add 192.168.1.50/24 dev eth0
sudo ip link set eth0 up
```

#### Verification:
```bash
ping -c 3 192.168.1.120
```

---

## 2. Radio Interfacing (Wouxun / Kenwood 2-Pin Data Port)

Most Wouxun, Baofeng, and Kenwood handhelds use the standard "K-type" 2-pin connector. The NET51TNC typically connects to this via an 8-pin Mini-DIN or RJ45 radio cable provided with the unit.

### Kenwood / Wouxun 2-Pin Plug Pinout:

```
        3.5mm Plug (SPK)                   2.5mm Plug (MIC / PTT)
         +---+---+-------+                   +---+---+-------+
         | T | R |   S   |                   | T | R |   S   |
         +---+---+-------+                   +---+---+-------+
           |       |                           |       |
         SPK+     GND                         MIC+    PTT / GND
```

| Pin | Connector | Function | NET-51TNC Connection |
| :--- | :--- | :--- | :--- |
| **SPK Out** | 3.5mm Tip | Audio from radio receiver | RX Audio In (AFSK demodulator) |
| **SPK GND** | 3.5mm Sleeve | Ground return | System Ground |
| **MIC In** | 2.5mm Tip | Audio to radio transmitter | TX Audio Out (AFSK modulator) |
| **PTT** | 2.5mm Ring / Sleeve | Push-To-Talk (pull to GND) | PTT Output (open-collector switch) |
| **MIC GND** | 2.5mm Sleeve | Ground return | System Ground |

---

## 3. Radio Settings & Tuning

To ensure clean 1200 baud AFSK packet decoding and transmission:

1. **Frequency:** Set to your national APRS frequency:
   * **Europe:** `144.800 MHz`
   * **North America:** `144.390 MHz`
   * **Australia / NZ:** `145.175 MHz`
2. **Volume / RX Audio Level (Crucial OH2FXD & BH4TDV Tip):** 
   * The 51TNC audio demodulator has very high sensitivity. If the transceiver volume is set too high, receiver audio noise keeps the demodulator locked in squelch-detect mode, which inhibits PTT transmission and causes packet decode failures.
   * **Tuning Method:** Turn the transceiver volume knob completely counter-clockwise to zero, then open it just a tiny fraction of a turn until the TNC's RXD LED flashes cleanly upon receiving an incoming APRS packet.
3. **Squelch (SQL):** Set squelch to **Level 1** (or open if using TNC software carrier detection).
4. **VOX:** Ensure **VOX is OFF** on the radio (PTT is controlled via hardware line).
5. **Battery Saver (Author FAQ 7 Warning):** Turn **Battery Saver OFF**! Handheld radios in battery-save mode sleep for ~1-second intervals, causing the first packet after an idle period to be missed. If battery saver cannot be disabled, set transmitting stations' PTT preamble delay (TXD) to at least 500 ms.
6. **Wide/Narrow Band:** Use **Wide FM (25 kHz)** for 1200 baud APRS.

---

## 4. Hardware Recovery & Field Troubleshooting (Author FAQ & Schematics)

* **Emergency Hardware Reset (Factory Defaults)**:
  1. Power off the TNC.
  2. Bridge/short pins **IO3** and **IO4** on the expansion header with tweezers or a jumper wire.
  3. Power on with the jumper in place. Both **RXD** and **TXD** LEDs will illuminate solid, indicating reset complete.
  4. Power off, remove the jumper, and power on. IP resets to `192.168.1.120`, Gateway `192.168.1.1`.
* **Bluetooth Pairing PIN**: Default code is **`1234`** (for models equipped with onboard Bluetooth module).
* **Multiple TNCs on Same Subnet**: If running multiple units, change the last byte of the MAC address in EEPROM to prevent ARP collisions.
* **Low-Audio Transceiver Mod (R36)**: If using low-deviation transceivers (e.g. Yaesu FT-8GR or certain HTs) that consistently fail to decode, replacing mainboard resistor **R36** with **10kΩ** boosts demodulator input level.
* **Hardware Squelch (SQL)**: STC MCU pin 29 (P4.6) connects via a 1N4148 diode to the radio DATA squelch line.
* **Firmware Flashing / ISP**: The board features an STC 8051 MCU flashed via standard CH340/CH341 USB-to-UART cables using `stcgal` or the official updater.

---

## 5. Power Supply

* **Input Voltage:** `8V – 15V DC` (standard 12V 1A DC adapter recommended, center-positive 5.5mm x 2.1mm barrel jack).
* **Current Consumption:** ~100mA – 250mA (idle / active decoding).
