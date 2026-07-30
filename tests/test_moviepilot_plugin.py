#!/opt/homebrew/bin/python3.12

from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = (
    PROJECT_ROOT / "moviepilot-plugin/plugins.v2/storagecleanup"
)


def load_bridge_module():
    spec = importlib.util.spec_from_file_location(
        "storagecleanup_bridge_client",
        PLUGIN_ROOT / "bridge_client.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


bridge_client = load_bridge_module()


class FakeResponse:
    status = 200

    def __init__(self, payload: dict):
        self.payload = payload

    def read(self, _: int) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class MoviePilotPluginTests(unittest.TestCase):
    def test_manifest_and_federation_entry_are_present(self):
        manifest = json.loads(
            (
                PROJECT_ROOT / "moviepilot-plugin/package.v2.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["StorageCleanup"]["system_version"], ">=2.14.6")
        self.assertEqual(manifest["StorageCleanup"]["version"], "1.0.3")
        self.assertTrue(
            (PLUGIN_ROOT / "dist/v1.0.3/assets/remoteEntry.js").is_file()
        )

    def test_bridge_uses_token_only_in_internal_headers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            token_path = Path(temp_dir) / "control-token"
            token_path.write_text("secret-value\n", encoding="utf-8")
            bridge = bridge_client.CleanupBridge(
                gateway="http://192.0.2.1:3000/control",
                token_file=str(token_path),
            )
            captured = {}

            @contextmanager
            def fake_urlopen(request, timeout):
                captured["request"] = request
                captured["timeout"] = timeout
                yield FakeResponse({"ok": True})

            with patch.object(bridge_client, "urlopen", fake_urlopen):
                status, payload = bridge.request(
                    "/v1/plan",
                    method="POST",
                    payload={"mode": "pause"},
                )

            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            request = captured["request"]
            self.assertEqual(
                request.get_header("X-pinas-bridge-token"),
                "secret-value",
            )
            self.assertEqual(
                request.get_header("X-pinas-session"),
                "secret-value",
            )
            self.assertNotIn(b"secret-value", request.data)

    def test_missing_bridge_token_fails_closed(self):
        bridge = bridge_client.CleanupBridge(token_file="/definitely/missing")
        with self.assertRaisesRegex(RuntimeError, "控制令牌不可用"):
            bridge.request("/health")

    def test_vue_page_exposes_all_three_execution_levels(self):
        source = (PLUGIN_ROOT / "src/provider.js").read_text(encoding="utf-8")
        for action in ("pause", "retire", "delete"):
            self.assertIn(f"{action}:", source)
        page = (
            PLUGIN_ROOT / "src/components/AppPage.vue"
        ).read_text(encoding="utf-8")
        self.assertEqual(page.count('<Teleport to="body">'), 1)
        teleported = page.split('<Teleport to="body">', 1)[1].split(
            "</Teleport>",
            1,
        )[0]
        for marker in (
            'class="action-bar"',
            'v-if="planOpen"',
            'v-if="gapOpen"',
            'v-if="recoveryOpen"',
        ):
            self.assertIn(marker, teleported)
        self.assertIn("position: fixed;", page)
        self.assertIn("inset: 0;", page)
        self.assertIn("place-items: center;", page)
        self.assertIn("max-height: 90vh;", page)
        self.assertIn("@media (max-width: 760px)", page)
        self.assertIn("grid-template-areas:", page)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr));", page)
        self.assertIn("env(safe-area-inset-bottom, 0px)", page)
        self.assertIn("max-height: calc(100dvh - 24px);", page)
        self.assertIn("confirmPhrase: plan.value.confirmPhrase", page)
        self.assertIn("acknowledgeSiteRisk", page)


if __name__ == "__main__":
    unittest.main()
