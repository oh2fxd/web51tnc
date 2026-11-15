"""
Integration tests for Web51TNC FastAPI endpoints and Explicit State Machines.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from server import app, tnc
from net51tnc_protocol import (
    ConnectionState,
    ConfigSyncState,
    build_eeprom,
    parse_eeprom,
)


class TestServerStateMachines(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        tnc.heard_stations.clear()
        tnc.longest_stations.clear()
        tnc.state = ConnectionState.DISCONNECTED
        tnc.connected = False
        tnc.connecting = False
        tnc.config_state = ConfigSyncState.UNKNOWN
        tnc.reboot_seconds_remaining = None
        tnc.raw_template = None
        tnc.last_config = None

    def test_status_endpoint_state_fields(self):
        res = self.client.get("/api/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("state", data)
        self.assertIn("config_state", data)
        self.assertIn("reboot_seconds_remaining", data)
        self.assertIn("connected", data)
        self.assertIn("connecting", data)
        self.assertEqual(data["state"], "disconnected")
        self.assertEqual(data["config_state"], "unknown")
        self.assertIsNone(data["reboot_seconds_remaining"])

    def test_agent_state_endpoint(self):
        res = self.client.get("/api/state")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["connection_state"], "disconnected")
        self.assertEqual(data["config_state"], "unknown")
        self.assertFalse(data["is_ready"])
        self.assertEqual(data["total_stations"], 0)
        self.assertEqual(data["active_stations"], 0)

    def test_stations_endpoint_freshness_filtering(self):
        now = datetime.now(timezone.utc)
        tnc.heard_stations["N0CALL-9"] = {
            "callsign": "N0CALL-9",
            "last_heard": (now - timedelta(minutes=3)).isoformat(),
            "packet_count": 5,
            "is_server": False,
        }
        tnc.heard_stations["N0CALL-7"] = {
            "callsign": "N0CALL-7",
            "last_heard": (now - timedelta(minutes=25)).isoformat(),
            "packet_count": 2,
            "is_server": False,
        }
        tnc.heard_stations["N0CALL-10"] = {
            "callsign": "N0CALL-10",
            "last_heard": (now - timedelta(minutes=90)).isoformat(),
            "packet_count": 10,
            "is_server": True,
        }

        res_all = self.client.get("/api/stations")
        self.assertEqual(res_all.status_code, 200)
        self.assertEqual(len(res_all.json()), 3)

        res_active = self.client.get("/api/stations?freshness=active")
        self.assertEqual(res_active.status_code, 200)
        data_active = res_active.json()
        self.assertEqual(len(data_active), 1)
        self.assertEqual(data_active[0]["callsign"], "N0CALL-9")
        self.assertEqual(data_active[0]["freshness"], "active")

        res_rf = self.client.get("/api/stations?rf_only=true")
        self.assertEqual(res_rf.status_code, 200)
        data_rf = res_rf.json()
        self.assertEqual(len(data_rf), 2)
        callsigns = [s["callsign"] for s in data_rf]
        self.assertIn("N0CALL-9", callsigns)
        self.assertIn("N0CALL-7", callsigns)
        self.assertNotIn("N0CALL-10", callsigns)

    def test_reboot_state_guards_against_conflicting_calls(self):
        tnc.state = ConnectionState.REBOOTING
        tnc.reboot_seconds_remaining = 5

        res_cfg = self.client.get("/api/config")
        self.assertEqual(res_cfg.status_code, 409)
        self.assertIn("rebooting", res_cfg.json()["detail"])

        res_cmd = self.client.post("/api/command/gate_rf_test")
        self.assertEqual(res_cmd.status_code, 409)
        self.assertIn("rebooting", res_cmd.json()["detail"])

    def test_write_config_refused_without_template(self):
        tnc.state = ConnectionState.CONNECTED
        tnc.connected = True
        tnc.writer = object()  # Dummy non-None writer
        tnc.raw_template = None

        res = self.client.post("/api/config", json={"callsign": "N0CALL", "ssid": 10})
        self.assertEqual(res.status_code, 409)
        self.assertIn("EEPROM template", res.json()["detail"])

    def test_write_config_validation_rejects_bad_input(self):
        res = self.client.post(
            "/api/config", json={"callsign": "INVALID_CALLSIGN_TOO_LONG", "ssid": 99}
        )
        self.assertEqual(res.status_code, 422)

    def test_passcode_and_raw_eeprom_endpoints(self):
        res_pass = self.client.get("/api/passcode/N0CALL-10")
        self.assertEqual(res_pass.status_code, 200)
        self.assertEqual(res_pass.json(), {"callsign": "N0CALL", "passcode": "13023"})

        res_no_eeprom = self.client.get("/api/eeprom/raw")
        self.assertEqual(res_no_eeprom.status_code, 404)

        tnc.raw_template = b"\x55" * 512
        res_eeprom = self.client.get("/api/eeprom/raw")
        self.assertEqual(res_eeprom.status_code, 200)
        self.assertEqual(len(res_eeprom.content), 512)
        self.assertEqual(res_eeprom.content, b"\x55" * 512)

    def test_first_eeprom_download_safekept_write_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tnc.backup_dir = tmpdir

            # 1. First download from device: callsign FIRST1
            first_bytes = build_eeprom({"callsign": "FIRST1", "ssid": 10})
            tnc._safekeep_eeprom(first_bytes, parse_eeprom(first_bytes))

            # 2. Second download/write: callsign SECOND
            second_bytes = build_eeprom({"callsign": "SECOND", "ssid": 5})
            tnc._safekeep_eeprom(second_bytes, parse_eeprom(second_bytes))

            # Verify eeprom_initial.bin was NEVER overwritten and still holds FIRST1
            res_init_json = self.client.get("/api/eeprom/initial")
            self.assertEqual(res_init_json.status_code, 200)
            self.assertEqual(res_init_json.json()["callsign"], "FIRST1")
            self.assertEqual(res_init_json.json()["ssid"], 10)

            res_init_raw = self.client.get("/api/eeprom/initial?raw=true")
            self.assertEqual(res_init_raw.status_code, 200)
            self.assertEqual(res_init_raw.content, first_bytes)

            # Verify eeprom_latest.bin has the newest state (SECOND)
            with open(os.path.join(tmpdir, "eeprom_latest.bin"), "rb") as f:
                latest_on_disk = f.read()
            self.assertEqual(parse_eeprom(latest_on_disk)["callsign"], "SECOND")

    def test_status_text_parser_ignores_arbitrary_v(self):
        tnc.latest_status = {
            "voltage": None,
            "server_ip": None,
            "server_port": None,
            "server_status": None,
        }
        updated = tnc._parse_status_text("Random comment with Version 2")
        self.assertFalse(updated)
        self.assertIsNone(tnc.latest_status["voltage"])

        updated_ok = tnc._parse_status_text("13.8V\r\n218.104.177.211\r\n14580\r\n5")
        self.assertTrue(updated_ok)
        self.assertEqual(tnc.latest_status["voltage"], "13.8V")
        self.assertEqual(tnc.latest_status["server_ip"], "218.104.177.211")
        self.assertEqual(tnc.latest_status["server_status"], "Connected / Login OK")

    def test_websocket_init_message(self):
        with self.client.websocket_connect("/ws") as websocket:
            data = websocket.receive_json()
            self.assertEqual(data["type"], "init")
            self.assertIn("state", data)
            self.assertIn("config_state", data)
            self.assertIn("stations", data)
            self.assertIn("longest_stations", data)

    def test_station_distance_and_longest_stations_hall_of_fame(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            orig_backup_dir = tnc.backup_dir
            tnc.backup_dir = tmpdir
            try:
                tnc.last_config = {
                    "callsign": "OH2FXD",
                    "gateway_lat": "6010.20N",
                    "gateway_lon": "02456.30E",
                }
                now = datetime.now(timezone.utc).isoformat()
                tnc.heard_stations["OH3TEST-9"] = {
                    "callsign": "OH3TEST-9",
                    "last_heard": now,
                    "packet_count": 1,
                    "is_server": False,
                    "lat": 61.4978,
                    "lon": 23.7610,
                }
                res_stations = self.client.get("/api/stations")
                self.assertEqual(res_stations.status_code, 200)
                st_list = res_stations.json()
                self.assertEqual(len(st_list), 1)
                self.assertIn("distance_km", st_list[0])
                self.assertGreater(st_list[0]["distance_km"], 155.0)
                self.assertIn("bearing_deg", st_list[0])

                # Record RF and IS entries in Hall of Fame
                pkt_rf = {
                    "source": "OH3TEST-9",
                    "is_server": False,
                    "lat": 61.4978,
                    "lon": 23.7610,
                    "path": "WIDE1-1",
                    "comment": "Tampere mobile",
                }
                self.assertTrue(
                    tnc._record_longest_station(
                        "OH3TEST-9",
                        pkt_rf,
                        tnc.heard_stations["OH3TEST-9"],
                        160.5,
                        336,
                        now,
                    )
                )
                # Shorter distance for same RF station should not overwrite
                self.assertFalse(
                    tnc._record_longest_station(
                        "OH3TEST-9",
                        pkt_rf,
                        tnc.heard_stations["OH3TEST-9"],
                        50.0,
                        336,
                        now,
                    )
                )

                res_hof = self.client.get("/api/stations/longest?rf_only=true")
                self.assertEqual(res_hof.status_code, 200)
                hof = res_hof.json()
                self.assertEqual(len(hof), 1)
                self.assertEqual(hof[0]["callsign"], "OH3TEST-9")
                self.assertEqual(hof[0]["max_distance_km"], 160.5)

                res_del = self.client.delete("/api/stations/longest")
                self.assertEqual(res_del.status_code, 200)
                self.assertEqual(self.client.get("/api/stations/longest").json(), [])
            finally:
                tnc.backup_dir = orig_backup_dir

    def test_wifi_status_endpoint(self):
        res = self.client.get("/api/wifi/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("mode", data)
        self.assertIn("ap_ssid", data)
        self.assertEqual(data["ap_ssid"], "Web51TNC-Setup")
        self.assertIn("needs_setup", data)


if __name__ == "__main__":
    unittest.main()
