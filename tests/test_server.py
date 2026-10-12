import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from server import AppServer, Console, dump_config, make_handler


ROOT = Path(__file__).resolve().parents[1]


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "HR650X.yaml"
        self.path.write_text((ROOT / "HR650X.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        self.console = Console(self.path, demo=True, environ={})
        self.httpd = AppServer(("127.0.0.1", 0), make_handler(self.console))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.console.stop_auto()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def request(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urllib.request.urlopen(req) as response:
                body = response.read().decode()
                if response.headers.get("Content-Type", "").startswith("application/json"):
                    return response.status, json.loads(body)
                return response.status, body
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            exc.close()
            return exc.code, json.loads(raw)

    def test_page_and_curve_load(self):
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("HR650X", body)
        self.assertIn("开始自动", body)
        status, state = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(state["demo"])
        self.assertEqual(len(state["config"]["bands"]), 7)
        self.assertFalse(state["has_password"])

    def test_probe_ignores_vrd_and_manual_sets_duty(self):
        status, state = self.request("POST", "/api/probe", {})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(state["sample"]["temp"], 37)
        self.assertLessEqual(state["sample"]["temp"], 70.05)
        self.assertEqual(state["sample"]["matched"], 2)
        status, state = self.request("POST", "/api/manual", {"speed": 33})
        self.assertEqual(status, 200)
        self.assertEqual(state["duty"], 33)
        self.assertEqual(self.console.client.duty, 33)

    def test_overlap_is_rejected_and_save_omits_password(self):
        status, body = self.request("POST", "/api/settings", {
            "bands": [
                {"low": 0, "high": 50, "speed": 10},
                {"low": 40, "high": 100, "speed": 80},
            ],
            "password": "s3cret",
        })
        self.assertEqual(status, 400)
        self.assertIn("重叠", body["error"])
        status, state = self.request("POST", "/api/settings", {
            "bands": [
                {"low": 0, "high": 40, "speed": 12},
                {"low": 40, "high": 100, "speed": 80},
            ],
            "password": "s3cret",
            "save": True,
        })
        self.assertEqual(status, 200)
        self.assertEqual(state["config"]["bands"][0]["speed"], 12)
        self.assertTrue(state["has_password"])
        written = self.path.read_text(encoding="utf-8")
        self.assertIn("speed: 12", written)
        self.assertIn("s3cret", written)

    def test_auto_loop_then_stop(self):
        status, _state = self.request("POST", "/api/settings", {"interval": 0.2})
        self.assertEqual(status, 200)
        status, state = self.request("POST", "/api/auto/start", {})
        self.assertEqual(status, 200)
        self.assertTrue(state["running"])
        deadline = time.monotonic() + 3
        sample = None
        while time.monotonic() < deadline:
            _status, state = self.request("GET", "/api/state")
            sample = state["sample"]
            if sample and state["duty"] is not None:
                break
            time.sleep(0.05)
        self.assertIsNotNone(sample)
        self.assertLessEqual(sample["temp"], 70.05)
        status, state = self.request("POST", "/api/auto/stop", {})
        self.assertEqual(status, 200)
        self.assertFalse(state["running"])
        self.assertEqual(state["duty"], 20)

    def test_real_mode_without_password_refuses_to_write(self):
        self.console.secret = None
        status, _state = self.request("POST", "/api/demo", {"enabled": False})
        self.assertEqual(status, 200)
        status, body = self.request("POST", "/api/probe", {})
        self.assertEqual(status, 400)
        self.assertIn("密码", body["error"])

    def test_dump_without_password_omits_the_field(self):
        text = dump_config(self.console.config)
        self.assertNotIn("password:", text)
        self.assertIn("fan_speeds:", text)


if __name__ == "__main__":
    unittest.main()
