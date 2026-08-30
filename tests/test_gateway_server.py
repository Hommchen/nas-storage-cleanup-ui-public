#!/opt/homebrew/bin/python3.12

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from gateway_server import (
    backend_for,
    bridge_client_allowed,
    client_allowed,
    request_allowed,
    upstream_timeout,
)


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

    def test_docker_bridge_requires_live_token_and_control_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token_file = Path(temp_dir) / "control-token"
            token_file.write_text("expected-token\n", encoding="utf-8")
            settings = {
                "bridge_network": "172.17.0.0/16",
                "bridge_token_file": str(token_file),
            }
            self.assertFalse(
                bridge_client_allowed("172.17.0.2", None, **settings)
            )
            self.assertFalse(
                bridge_client_allowed("172.17.0.2", "wrong", **settings)
            )
            self.assertTrue(
                bridge_client_allowed(
                    "172.17.0.2",
                    "expected-token",
                    **settings,
                )
            )
            self.assertTrue(
                request_allowed(
                    "172.17.0.2",
                    is_control=True,
                    bridge_token="expected-token",
                    **settings,
                )
            )
            self.assertFalse(
                request_allowed(
                    "172.17.0.2",
                    is_control=False,
                    bridge_token="expected-token",
                    **settings,
                )
            )

    def test_control_session_requires_same_origin_browser_metadata(self):
        settings = {
            "bridge_network": "172.17.0.0/16",
            "bridge_token_file": "/missing",
            "public_origins": {"http://192.0.2.1:3000"},
        }
        self.assertFalse(
            request_allowed(
                "192.168.3.42",
                is_control=True,
                bridge_token=None,
                control_path="/v1/session",
                method="GET",
                **settings,
            )
        )
        self.assertFalse(
            request_allowed(
                "192.168.3.42",
                is_control=True,
                bridge_token=None,
                request_origin="https://attacker.invalid",
                control_path="/v1/session",
                method="GET",
                **settings,
            )
        )
        self.assertTrue(
            request_allowed(
                "192.168.3.42",
                is_control=True,
                bridge_token=None,
                request_origin="http://192.0.2.1:3000",
                control_path="/v1/session",
                method="GET",
                **settings,
            )
        )
        self.assertTrue(
            request_allowed(
                "192.168.3.42",
                is_control=True,
                bridge_token=None,
                request_referer="http://192.0.2.1:3000/storage",
                control_path="/v1/session",
                method="GET",
                **settings,
            )
        )
        self.assertTrue(
            request_allowed(
                "192.168.3.42",
                is_control=True,
                bridge_token=None,
                sec_fetch_site="same-origin",
                control_path="/v1/session",
                method="GET",
                **settings,
            )
        )

    def test_control_health_and_public_snapshot_remain_read_only_public(self):
        settings = {
            "bridge_network": "172.17.0.0/16",
            "bridge_token_file": "/missing",
        }
        for path in ("/health", "/v1/snapshot"):
            with self.subTest(path=path):
                self.assertTrue(
                    request_allowed(
                        "192.168.3.42",
                        is_control=True,
                        bridge_token=None,
                        control_path=path,
                        method="GET",
                        **settings,
                    )
                )

    def test_lan_access_does_not_require_bridge_token(self):
        self.assertTrue(
            request_allowed(
                "192.168.3.42",
                is_control=False,
                bridge_token=None,
                bridge_network="172.17.0.0/16",
                bridge_token_file="/missing",
            )
        )


if __name__ == "__main__":
    unittest.main()
