# Reverse Engineering & Firmware Extraction Guide

This guide documents the toolchain, methodology, and practical workflows used to reverse-engineer embedded devices, proprietary configuration software, and firmware binaries—with specific reference to the **NET-51TNC** amateur radio iGate/TNC and general ham radio / IoT hardware.

---

## 1. Toolchain & Installation

### A. Python Analysis Stack (User Space - No Sudo Required)
The following Python packages provide decompilation, parsing, and microcontroller flashing capabilities:

```bash
pip install --user --break-system-packages dnfile pefile dncil stcgal
```

| Tool | Purpose |
| :--- | :--- |
| `dnfile` | Parses .NET PE binaries, metadata tables (`#~`), and heaps (`#Strings`, `#US`, `#Blob`, `#GUID`). |
| `pefile` | Low-level Portable Executable header and section analysis. |
| `dncil` | Mandiant's Common Intermediate Language (CIL) disassembler. |
| `stcgal` | Open-source ISP flash tool for STC 8051 microcontrollers (the core MCU in 51TNC). |
| `binwalk` | Firmware image scanner and extractor (identifies compressed streams and file systems). |

### B. System Packages (Run with Sudo)
To install system-level disassemblers, firmware converters, and serial tools, run the following command in your terminal:

```bash
sudo apt update && sudo apt install -y mono-utils radare2 srecord minicom
```

| Tool | Executable | Purpose |
| :--- | :--- | :--- |
| **mono-utils** | `monodis`, `pedump` | Disassembles .NET assemblies into clean IL text representations. |
| **radare2** | `r2`, `rabin2`, `rasm2` | Multi-architecture reverse-engineering framework (ARM, 8051, AVR, x86). |
| **srecord** | `srec_cat`, `srec_cmp` | Converts between Intel HEX (`.hex`), Motorola S-Record (`.s19`), and raw binary (`.bin`). |
| **minicom** | `minicom` | Serial terminal for debugging hardware TNC ports and bootloaders. |

---

## 2. Walkthrough: How NET-51TNC Was Reverse-Engineered

### Step 1: Binary Identification
First, determine the binary type and architecture:
```bash
file "APRS 51TNC SETUP _ENGLISH_20140701.exe"
```
*Result*: `PE32 executable (GUI) Intel 80386 Mono/.Net assembly, for MS Windows`.

### Step 2: Metadata & Heaps Inspection
.NET binaries store code in CIL bytecodes inside ECMA-335 metadata streams rather than native x86 machine instructions:
- `#~`: Metadata tables (types, methods, fields, references).
- `#Strings`: Type and member names.
- `#US`: User Strings (all string literals in code).
- `#Blob`: Signatures and raw data blobs.

### Step 3: Event Handler Mapping
By listing non-compiler methods in the main Form, we mapped every control event to its functionality:

| Control Handler | Action / Command | Description |
| :--- | :--- | :--- |
| `Button1_Click` | `#RESET` | Soft reboot command sent to TNC. |
| `Button2_Click` | `#GATE_RF_TEST` | Forces manual RF transmission of the Gateway beacon. |
| `Button3_Click` | `#WX_RF_TEST` | Forces manual RF transmission of the Weather beacon. |
| `Button4_Click` | `#HELLO` | Requests the active 512-byte configuration from TNC EEPROM. |
| `Button5_Click` | Connect / Disconnect | Toggles TCP Winsock connection to port `60000`. |
| `Button6_Click` | Write EEPROM | Serializes 512 bytes and transmits update stream. |
| `Button7_Click` | `#INSTALL` | Hardware factory reset / restore default settings. |
| `Button8_Click` | `#GATE_IS_TEST` | Injects Gateway beacon directly to APRS-IS. |
| `Button9_Click` | `#WX_IS_TEST` | Injects Weather beacon directly to APRS-IS. |
| `Button10_Click` | `TextBox.Clear()` | Clears the live packet terminal log. |
| `AxWinsock1_DataArrival` | Packet & Telemetry Parser | Decodes incoming RF/IS packets and weather telemetry. |

### Step 4: Uncovering the Big-Endian uint16 Bug
In `Button6_Click`, beacon intervals were serialized to array positions. Decompiling the CIL instruction stream revealed:
```cil
ldc.i4.s   128          // EEPROM offset 128 (RF Beacon timer high byte)
ldloc.s    V_10         // Beacon interval in seconds (0 - 65535)
ldc.i4     256
div                     // High byte = interval / 256
stelem.i1

ldc.i4.s   129          // EEPROM offset 129 (RF Beacon timer low byte)
ldloc.s    V_10
ldc.i4     256
rem                     // Low byte = interval % 256
stelem.i1
```
*Discovery*: Beacon timers are **16-bit Big-Endian integers** (`0–65535` seconds), not 32-bit Little-Endian integers as previously assumed.

---

## 3. Firmware & Hardware Extraction (Microcontroller Layer)

### A. Hardware Identification
The "51" in **NET-51TNC** denotes an **8051-core microcontroller** (typically STC microcontroller series such as STC89C516, STC12C5A60S2, or STC15W series with built-in hardware UART and internal EEPROM).

### B. Extracting / Flashing via UART ISP
STC microcontrollers feature a factory ROM bootloader activated by cycling VCC while holding RXD low:
```bash
# Probe STC MCU model and status over serial port
stcgal -P stc12 -p /dev/ttyUSB0

# Dump EEPROM / flash contents (if lockbits allow reading)
stcgal -P stc12 -p /dev/ttyUSB0 -D dump_eeprom.bin

# Flash new or patched firmware
stcgal -P stc12 -p /dev/ttyUSB0 firmware.hex
```

### C. EEPROM & SPI Flash Extraction
If the device uses an external 24C series I2C EEPROM (e.g. 24C04 / 24C08 / 24C16) or SPI flash:
1. Connect via SOIC-8 clip or desolder chip.
2. Read using a programmer (`minipro` with TL866 / T48 or `flashrom` with Raspberry Pi / FT232H):
   ```bash
   minipro -p "AT24C04" -r eeprom_dump.bin
   ```
3. Analyze hex layout with `xxd`:
   ```bash
   xxd -g 1 -c 16 eeprom_dump.bin | head -n 32
   ```

### D. Firmware Image Disassembly with Radare2
To disassemble raw 8051 or ARM Cortex-M firmware dumps:
```bash
# 8051 Microcontroller binary
r2 -a 8051 -b 8 firmware.bin

# ARM Cortex-M (Thumb mode, base address 0x08000000)
r2 -a arm -b 16 -m 0x08000000 firmware.bin
```

---

## 4. Automated Python RE Inspector (`tools/re_tnc_analyzer.py`)

A dedicated CLI analyzer is included in this repository under `tools/re_tnc_analyzer.py`.

### Usage Examples:
```bash
# 1. List all methods and their RVAs
tools/re_tnc_analyzer.py "/path/to/binary.exe" --list-methods

# 2. Extract string constants from a specific method
tools/re_tnc_analyzer.py "/path/to/binary.exe" --strings Button6_Click

# 3. Disassemble CIL bytecode instructions
tools/re_tnc_analyzer.py "/path/to/binary.exe" --disasm Button1_Click

# 4. Trace all EEPROM array store offsets in Button6_Click
tools/re_tnc_analyzer.py "/path/to/binary.exe" --trace-eeprom
```
