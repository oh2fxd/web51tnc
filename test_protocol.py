"""
Unit tests for NET-51TNC Protocol Parser & Serializer
"""

import re
import os
import unittest
from datetime import datetime, timezone, timedelta
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
)


class TestNet51TNCProtocol(unittest.TestCase):
    def test_commands(self):
        self.assertEqual(CMD_READ, b"#HELLO")
        self.assertEqual(CMD_RESET, b"#RESET")
        self.assertEqual(CMD_INSTALL, b"#INSTALL")
        self.assertEqual(CMD_GATE_RF_TEST, b"#GATE_RF_TEST")
        self.assertEqual(CMD_GATE_IS_TEST, b"#GATE_IS_TEST")
        self.assertEqual(CMD_WX_RF_TEST, b"#WX_RF_TEST")
        self.assertEqual(CMD_WX_IS_TEST, b"#WX_IS_TEST")

    def test_eeprom_roundtrip_and_template_preservation(self):
        # Populate template with sentinel bytes at undocumented offsets (e.g. 50, 60, 134, 420)
        template = bytearray(512)
        template[50] = 0xAA
        template[60] = 0xBB
        template[134] = 0xCC
        template[420] = 0xDD

        original_config = {
            "callsign": "N0CALL",
            "ssid": 9,
            "passcode": "24680",
            "ip_address": "192.168.1.125",
            "netmask": "255.255.255.0",
            "gateway": "192.168.1.1",
            "mac_address": "00:08:DC:11:22:33",
            "server_a": "218.104.177.211",
            "server_b": "192.168.1.50",
            "server_c": "127.0.0.1",
            "allow_is_to_rf": True,
            "server_mode": "always_a",
            "timeout_restart": True,
            "hellocq_enabled": True,
            "lcd_type": "Color 320x240",
            "lcd_type_code": 2,
            "pm25_enabled": True,
            "aprs_filter": "m/50",
            "gateway_beacon_is_sec": 600,
            "gateway_beacon_rf_sec": 1800,
            "gateway_path_idx": 1,
            "gateway_path": "WIDE1-1",
            "gateway_report_voltage": True,
            "gateway_ssid": 10,
            "gateway_packet_type": "!",
            "gateway_lat": "6010.50N",
            "gateway_symbol_table": "/",
            "gateway_lon": "02456.20E",
            "gateway_symbol_code": "r",
            "gateway_comment": "Helsinki APRS Gateway",
            "weather_beacon_is_sec": 300,
            "weather_beacon_rf_sec": 900,
            "weather_path_idx": 2,
            "weather_path": "WIDE1-1,WIDE2-1",
            "weather_report_voltage": True,
            "weather_ssid": 13,
            "weather_use_gps_coords": True,
            "weather_lcd_display": True,
            "weather_packet_type": "=",
            "weather_lat": "6010.55N",
            "weather_symbol_table": "/",
            "weather_lon": "02456.25E",
            "weather_symbol_code": "_",
            "weather_telemetry_format": "c000s000g000t050r000p000h50b10050",
            "weather_comment": "Helsinki Weather Station",
            "ptt_delay_code": 2,
            "min_tx_interval_code": 1,
            "tx_level": "ICOM_HIGH",
            "baud_mode": "Clever 9600",
            "digi_wide1": True,
            "digi_wide2": True,
            "insert_gateway_call": True,
            "tx_waiting_code": 2,
            "wait_channel_idle": True,
            "sql_hardware_detect": True,
            "rf_lcd_display": True,
            "gps_baud": 9600,
            "gps_lcd_display": True,
            "gps_tracks": True,
            "gps_beacon_interval": 120,
            "gps_beacon_mode": "SmartBeacon",
            "gps_ssid": 9,
            "gps_path_idx": 1,
            "gps_path": "WIDE1-1",
            "gps_packet_type": "!",
            "gps_symbol_table": "/",
            "gps_symbol_code": "#",
            "report_voltage": True,
            "gps_offset_enabled": True,
            "gps_lat_offset_neg": False,
            "gps_lat_offset": 12,
            "gps_lon_offset_neg": True,
            "gps_lon_offset": 34,
            "gps_custom_info": "Mobile Tracker",
        }

        raw = build_eeprom(original_config, template=bytes(template))
        self.assertEqual(len(raw), 512)

        # Verify template sentinel bytes were preserved untouched
        self.assertEqual(raw[50], 0xAA)
        self.assertEqual(raw[60], 0xBB)
        self.assertEqual(raw[134], 0xCC)
        self.assertEqual(raw[420], 0xDD)

        # Check raw byte positions match the reverse-engineered specification
        self.assertEqual(raw[:6], b"N0CALL")
        self.assertEqual(raw[7], 9)
        self.assertEqual(raw[9:14], b"24680")
        self.assertEqual(list(raw[16:20]), [192, 168, 1, 125])
        self.assertEqual(raw[51], 2)
        self.assertEqual(raw[52], 1)
        self.assertEqual(raw[128], 0x02)
        self.assertEqual(raw[129], 0x58)
        self.assertEqual(raw[130], 0x07)
        self.assertEqual(raw[131], 0x08)
        self.assertEqual(raw[132], 1)
        self.assertEqual(raw[133], 1)
        self.assertEqual(raw[135], 10)
        self.assertEqual(raw[144], ord("!"))
        self.assertEqual(raw[145:153], b"6010.50N")
        self.assertEqual(raw[153], ord("/"))
        self.assertEqual(raw[154:163], b"02456.20E")
        self.assertEqual(raw[163], ord("r"))

        self.assertEqual(raw[256], 0x01)
        self.assertEqual(raw[257], 0x2C)
        self.assertEqual(raw[258], 0x03)
        self.assertEqual(raw[259], 0x84)
        self.assertEqual(raw[263], 13)
        self.assertEqual(raw[272], ord("="))
        self.assertEqual(raw[273:281], b"6010.55N")
        self.assertEqual(raw[281], ord("/"))
        self.assertEqual(raw[282:291], b"02456.25E")
        self.assertEqual(raw[291], ord("_"))

        self.assertEqual(raw[387], 1)
        self.assertEqual(raw[388], 3)
        self.assertEqual(raw[389], 1)
        self.assertEqual(raw[390], 1)
        self.assertEqual(raw[391], 1)
        self.assertEqual(raw[393], 1)
        self.assertEqual(raw[394], 1)
        self.assertEqual(raw[395], 1)

        self.assertEqual(raw[396], 1)
        self.assertEqual(raw[397], 1)
        self.assertEqual(raw[398], 1)
        self.assertEqual(raw[400], 0x00)
        self.assertEqual(raw[401], 0x78)
        self.assertEqual(raw[402], 2)
        self.assertEqual(raw[403], 9)
        self.assertEqual(raw[405], ord("!"))
        self.assertEqual(raw[406], ord("/"))
        self.assertEqual(raw[407], ord("#"))
        self.assertEqual(raw[408], 1)

        parsed = parse_eeprom(raw)
        self.assertEqual(parsed["callsign"], "N0CALL")
        self.assertEqual(parsed["ssid"], 9)
        self.assertEqual(parsed["passcode"], "24680")
        self.assertEqual(parsed["ip_address"], "192.168.1.125")
        self.assertEqual(parsed["gateway_beacon_is_sec"], 600)
        self.assertEqual(parsed["gateway_beacon_rf_sec"], 1800)
        self.assertEqual(parsed["gateway_path"], "WIDE1-1")
        self.assertEqual(parsed["gateway_report_voltage"], True)
        self.assertEqual(parsed["gateway_ssid"], 10)
        self.assertEqual(parsed["gateway_packet_type"], "!")
        self.assertEqual(parsed["gateway_lat"], "6010.50N")
        self.assertEqual(parsed["gateway_symbol_table"], "/")
        self.assertEqual(parsed["gateway_lon"], "02456.20E")
        self.assertEqual(parsed["gateway_symbol_code"], "r")
        self.assertEqual(parsed["weather_beacon_is_sec"], 300)
        self.assertEqual(parsed["weather_beacon_rf_sec"], 900)
        self.assertEqual(parsed["weather_ssid"], 13)
        self.assertEqual(parsed["weather_packet_type"], "=")
        self.assertEqual(parsed["weather_lat"], "6010.55N")
        self.assertEqual(parsed["weather_symbol_table"], "/")
        self.assertEqual(parsed["weather_lon"], "02456.25E")
        self.assertEqual(parsed["weather_symbol_code"], "_")
        self.assertEqual(parsed["tx_level"], "ICOM_HIGH")
        self.assertEqual(parsed["baud_mode"], "Clever 9600")
        self.assertEqual(parsed["gps_baud"], 9600)
        self.assertEqual(parsed["gps_beacon_interval"], 120)
        self.assertEqual(parsed["gps_beacon_mode"], "SmartBeacon")
        self.assertEqual(parsed["gps_ssid"], 9)
        self.assertEqual(parsed["gps_packet_type"], "!")
        self.assertEqual(parsed["gps_symbol_table"], "/")
        self.assertEqual(parsed["gps_symbol_code"], "#")

    def test_build_eeprom_rejects_invalid_ip(self):
        with self.assertRaises(ValueError):
            build_eeprom({"ip_address": "999.999.1.1"})

    def test_compute_aprs_passcode(self):
        # Standard APRS-IS 16-bit hash algorithm
        code = compute_aprs_passcode("N0CALL")
        self.assertTrue(0 <= code <= 32767)
        self.assertEqual(compute_aprs_passcode("N0CALL-10"), code)
        self.assertEqual(compute_aprs_passcode("N0CALL"), 13023)

    def test_parse_weather_telemetry(self):
        raw = "c220s004g008t068r001p005h65b10132"
        data = parse_weather_telemetry(raw)
        self.assertIsNotNone(data)
        self.assertEqual(data["wind_direction_deg"], 220)
        self.assertEqual(data["wind_speed_mph"], 4)
        self.assertEqual(data["wind_gust_mph"], 8)
        self.assertEqual(data["temp_f"], 68)
        self.assertEqual(data["temp_c"], 20.0)
        self.assertEqual(data["rain_1h_mm"], 0.3)
        self.assertEqual(data["rain_24h_mm"], 1.3)
        self.assertEqual(data["humidity_pct"], 65)
        self.assertEqual(data["pressure_hpa"], 1013.2)

    def test_parse_weather_negative_temp_low_pressure_and_h00(self):
        raw = "c180s005g010t-05r000p000h00b09980"
        data = parse_weather_telemetry(raw)
        self.assertIsNotNone(data)
        self.assertEqual(data["temp_f"], -5)
        self.assertAlmostEqual(data["temp_c"], -20.6, places=1)
        self.assertEqual(data["humidity_pct"], 100)
        self.assertEqual(data["pressure_hpa"], 998.0)

    def test_parse_aprs_packet_rf(self):
        line = "N0CALL-9>APRS,WIDE1-1:=6010.50N/02456.20E#Antigravity Portable"
        pkt = parse_aprs_packet(line)
        self.assertTrue(pkt["is_packet"])
        self.assertFalse(pkt["is_server"])
        self.assertEqual(pkt["source"], "N0CALL-9")
        self.assertEqual(pkt["dest"], "APRS")
        self.assertEqual(pkt["path"], "WIDE1-1")
        self.assertAlmostEqual(pkt["lat"], 60.175, places=3)
        self.assertAlmostEqual(pkt["lon"], 24.93667, places=3)
        self.assertEqual(pkt["symbol_table"], "/")
        self.assertEqual(pkt["symbol_code"], "#")
        self.assertEqual(pkt["comment"], "Antigravity Portable")

    def test_parse_aprs_packet_is(self):
        line = "# N0CALL-10>APRS,TCPIP*:=6010.50N/02456.20E&Home iGate"
        pkt = parse_aprs_packet(line)
        self.assertTrue(pkt["is_packet"])
        self.assertTrue(pkt["is_server"])
        self.assertEqual(pkt["source"], "N0CALL-10")
        self.assertEqual(pkt["symbol_code"], "&")
        self.assertEqual(pkt["comment"], "Home iGate")

    def test_parse_compressed_position(self):
        def b91_encode(value: int, width: int) -> str:
            chars = []
            for _ in range(width):
                chars.append(chr(33 + value % 91))
                value //= 91
            return "".join(reversed(chars))

        lat, lon = 60.175, 24.93667
        yy = round((90.0 - lat) * 380926.0)
        xx = round((180.0 + lon) * 190463.0)
        payload = (
            "=/"
            + b91_encode(yy, 4)
            + b91_encode(xx, 4)
            + ">"
            + "SS"
            + "A"
            + "Compressed car"
        )
        line = "N0CALL-9>APRS,WIDE1-1:" + payload

        pkt = parse_aprs_packet(line)
        self.assertTrue(pkt["is_packet"])
        self.assertTrue(pkt.get("compressed"))
        self.assertAlmostEqual(pkt["lat"], lat, places=3)
        self.assertAlmostEqual(pkt["lon"], lon, places=3)
        self.assertEqual(pkt["symbol_table"], "/")
        self.assertEqual(pkt["symbol_code"], ">")
        self.assertEqual(pkt["comment"], "Compressed car")

    def test_parse_mice_position(self):
        # OH2ABC-9>V01P50,WIDE1-1:`4T2l!x>/]Mobile
        # V01P50 -> 60 deg 10.50 min N (char 4 'P' >= 'P' -> North), E (char 6 '0' < 'P' -> East)
        line = "OH2ABC-9>V01P50,WIDE1-1:`4T2l!x>/]Mobile"
        pkt = parse_aprs_packet(line)
        self.assertTrue(pkt["is_packet"])
        self.assertTrue(pkt.get("mice"))
        self.assertAlmostEqual(pkt["lat"], 60.175, places=3)
        self.assertIsNotNone(pkt["lon"])
        self.assertEqual(pkt["symbol_code"], ">")
        self.assertEqual(pkt["symbol_table"], "/")

    def test_status_packet_does_not_false_match_compressed(self):
        line = "OH2ABC>APRS,WIDE1-1:>Status update: all systems nominal 123456"
        pkt = parse_aprs_packet(line)
        self.assertIsNone(pkt["lat"])
        self.assertIsNone(pkt["lon"])

    def test_uncompressed_still_wins(self):
        line = "N0CALL-9>APRS,WIDE1-1:=6010.50N/02456.20E#Uncompressed"
        pkt = parse_aprs_packet(line)
        self.assertAlmostEqual(pkt["lat"], 60.175, places=3)
        self.assertNotIn("compressed", pkt)

    def test_tnc_direction_prefixes_and_ttl_garmin_rejection(self):
        # 1. TTL->GARMIN serial waypoint line must NOT be falsely parsed as an APRS station "TTL-"
        garmin_line = "TTL->GARMIN: $GPWPL,6012.00,N,02500.00,E,N0CALL-9*3F"
        pkt_garmin = parse_aprs_packet(garmin_line)
        self.assertFalse(pkt_garmin["is_packet"])

        # 2. RF ->TNC : prefixed packet must be parsed as an RF packet from N0CALL-9
        rf_line = (
            "RF ->TNC : N0CALL-9>APID98,WIDE1-1:!6012.00N/02500.00E*000/000/A=000537"
        )
        pkt_rf = parse_aprs_packet(rf_line)
        self.assertTrue(pkt_rf["is_packet"])
        self.assertEqual(pkt_rf["source"], "N0CALL-9")
        self.assertEqual(pkt_rf["dest"], "APID98")
        self.assertEqual(pkt_rf["path"], "WIDE1-1")
        self.assertFalse(pkt_rf["is_server"])
        self.assertEqual(pkt_rf["tnc_tag"], "RF->TNC")
        self.assertEqual(pkt_rf["symbol_table"], "/")
        self.assertEqual(pkt_rf["symbol_code"], "*")
        self.assertAlmostEqual(pkt_rf["lat"], 60.2, places=4)
        self.assertAlmostEqual(pkt_rf["lon"], 25.0, places=4)

        # 3. GATE ->IS: local gateway beacon
        gate_line = "GATE ->IS: N0CALL-10>APET51,WIDE1-1:!6010.20N/02456.30Er"
        pkt_gate = parse_aprs_packet(gate_line)
        self.assertTrue(pkt_gate["is_packet"])
        self.assertEqual(pkt_gate["source"], "N0CALL-10")
        self.assertTrue(pkt_gate["is_server"])
        self.assertEqual(pkt_gate["tnc_tag"], "GATE->IS")
        self.assertEqual(pkt_gate["symbol_code"], "r")

        # 4. # aprsc server banner must be non-packet
        aprsc_line = (
            "# aprsc 3.0.1-g79fe075 15 Nov 2026 03:58:23 GMT T2PRT 193.137.237.12:14580"
        )
        pkt_aprsc = parse_aprs_packet(aprsc_line)
        self.assertFalse(pkt_aprsc["is_packet"])

    def test_html_contains_all_js_element_ids(self):
        base = os.path.dirname(__file__)
        with open(os.path.join(base, "static", "app.js"), encoding="utf-8") as f:
            js = f.read()
        with open(os.path.join(base, "static", "index.html"), encoding="utf-8") as f:
            html = f.read()

        js_ids = set(
            re.findall(r'(?:getElementById|setVal|setChk|getVal|getChk)\("([^"]+)"', js)
        )
        html_ids = set(re.findall(r'id="([^"]+)"', html))
        missing = js_ids - html_ids
        self.assertEqual(
            missing, set(), f"Missing DOM IDs in index.html: {sorted(missing)}"
        )


class TestStateMachines(unittest.TestCase):
    def test_station_freshness_lifecycle(self):
        now = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)

        t_active = (now - timedelta(minutes=5)).isoformat()
        state, age = compute_station_freshness(t_active, now)
        self.assertEqual(state, StationFreshness.ACTIVE)
        self.assertAlmostEqual(age, 300.0, places=1)

        t_recent = (now - timedelta(minutes=30)).isoformat()
        state, age = compute_station_freshness(t_recent, now)
        self.assertEqual(state, StationFreshness.RECENT)
        self.assertAlmostEqual(age, 1800.0, places=1)

        t_stale = (now - timedelta(minutes=90)).isoformat()
        state, age = compute_station_freshness(t_stale, now)
        self.assertEqual(state, StationFreshness.STALE)
        self.assertAlmostEqual(age, 5400.0, places=1)

        state, age = compute_station_freshness("", now)
        self.assertEqual(state, StationFreshness.STALE)

    def test_stream_framer_line_fragmentation(self):
        framer = StreamFramer()

        chunk1 = b"N0CALL-9>APRS,WIDE1-1:=6010."
        res1 = framer.feed(chunk1)
        self.assertEqual(res1["lines"], [])

        chunk2 = b"50N/02456.20E#Test comment\r\nN0CALL-10>APRS:"
        res2 = framer.feed(chunk2)
        self.assertEqual(len(res2["lines"]), 1)
        self.assertEqual(
            res2["lines"][0], "N0CALL-9>APRS,WIDE1-1:=6010.50N/02456.20E#Test comment"
        )

        chunk3 = b"!6010.55N/02456.25E&iGate\r\n"
        res3 = framer.feed(chunk3)
        self.assertEqual(len(res3["lines"]), 1)
        self.assertEqual(res3["lines"][0], "N0CALL-10>APRS:!6010.55N/02456.25E&iGate")

    def test_stream_framer_config_buffering_and_in_flight_prefix(self):
        framer = StreamFramer()
        framer.set_state(StreamFramerState.READING_CONFIG)

        # In-flight packet arrives BEFORE the #HELLO response
        in_flight = b"OH1XX>APRS:!6000.00N/02400.00E-hi\r\n"
        dummy_payload = b"#HELLO" + (b"\x42" * 512)
        part1 = in_flight + dummy_payload[:200]
        part2 = dummy_payload[200:] + b"13.8V\r\nServer is connected\r\n"

        res1 = framer.feed(part1)
        self.assertIsNone(res1["config_ready"])
        self.assertEqual(res1["lines"], ["OH1XX>APRS:!6000.00N/02400.00E-hi"])

        res2 = framer.feed(part2)
        self.assertIsNotNone(res2["config_ready"])
        self.assertEqual(len(res2["config_ready"]), 518)
        self.assertTrue(res2["config_ready"].startswith(b"#HELLO"))
        self.assertEqual(res2["config_ready"][6:], b"\x42" * 512)
        self.assertEqual(res2["lines"], ["13.8V", "Server is connected"])
        self.assertEqual(framer.state, StreamFramerState.STREAM)

    def test_aprs_coord_to_decimal_and_distance_bearing(self):
        lat = aprs_coord_to_decimal("6010.20N", is_lon=False)
        lon = aprs_coord_to_decimal("02456.30E", is_lon=True)
        self.assertAlmostEqual(lat, 60.17, places=4)
        self.assertAlmostEqual(lon, 24.93833, places=4)
        self.assertIsNone(aprs_coord_to_decimal("9999.99N", is_lon=False))
        self.assertIsNone(aprs_coord_to_decimal("", is_lon=True))

        # Helsinki (60.17, 24.94) to Tampere (61.50, 23.76) ~ 160 km NNW
        dist = haversine_km(60.17, 24.93833, 61.4978, 23.7610)
        brng = bearing_deg(60.17, 24.93833, 61.4978, 23.7610)
        self.assertGreater(dist, 155.0)
        self.assertLess(dist, 165.0)
        self.assertGreater(brng, 320)
        self.assertLess(brng, 350)


if __name__ == "__main__":
    unittest.main()
