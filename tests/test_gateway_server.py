#!/opt/homebrew/bin/python3.12

from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from gateway_server import backend_for, client_allowed, upstream_timeout


class GatewayServerTests(unittest.TestCase):
    def test_control_path_is_stripped_and_keeps_query(self):
        self.assertEqual(
            backend_for("/control/v1/snapshot?fresh=1"),
            ("127.0.0.1", 8765, "/v1/snapshot?fresh=1", True),
        )
        self.assertEqual(
            backend_for("/control"),
            ("127.0.0.1", 8765, "/", True),
        )

    def test_web_paths_go_only_to_internal_frontend(self):
        self.assertEqual(
            backend_for("/assets/app.js"),
            ("127.0.0.1", 3001, "/assets/app.js", False),
        )
        self.assertEqual(
            backend_for("/controller"),
            ("127.0.0.1", 3001, "/controller", False),
        )

    def test_only_lan_and_loopback_clients_are_allowed(self):
        self.assertTrue(client_allowed("192.168.3.42"))
        self.assertTrue(client_allowed("127.0.0.1"))
        self.assertFalse(client_allowed("192.168.4.42"))
        self.assertFalse(client_allowed("100.64.0.1"))
        self.assertFalse(client_allowed("not-an-ip"))

    def test_control_upstream_has_time_for_qb_readback(self):
        self.assertEqual(upstream_timeout(True), 900)
        self.assertEqual(upstream_timeout(False), 30)


if __name__ == "__main__":
    unittest.main()
