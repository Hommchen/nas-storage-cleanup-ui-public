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
    PROJECT_ROOT / "plugins.v2/storagecleanup"
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
                PROJECT_ROOT / "package.v2.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["StorageCleanup"]["system_version"], ">=2.14.6")
        self.assertEqual(manifest["StorageCleanup"]["version"], "1.0.21")
        self.assertIn("自动发现 MoviePilot", manifest["StorageCleanup"]["description"])
        self.assertIn(
            "一键清理全链路耦合",
            (PLUGIN_ROOT / "__init__.py").read_text(encoding="utf-8"),
        )
        config_page = (PLUGIN_ROOT / "src/components/Config.vue").read_text(
            encoding="utf-8",
        )
        self.assertNotIn("SSH 目标", config_page)
        self.assertNotIn("ssh_host", config_page)
        self.assertNotIn("插件不会 SSH 登录 NAS", config_page)
        self.assertNotIn("首次使用按这 3 步", config_page)
        self.assertIn("一般无需填写，先点“自动识别”；识别失败再用手动配置。", config_page)
        self.assertIn("手动配置（自动识别失败时使用）", config_page)
        self.assertIn("打开手动配置", config_page)
        self.assertIn("应用识别结果并验证", config_page)
        self.assertIn("候选不唯一时不会自动猜", config_page)
        self.assertIn("候选：{{ (item.candidates || []).join('；') }}", config_page)
        self.assertIn("!discovery.value?.ready", config_page)
        self.assertIn(":disabled=\"saving || !discovery.ready\"", config_page)
        app_page = (PLUGIN_ROOT / "src/components/AppPage.vue").read_text(
            encoding="utf-8",
        )
        self.assertIn("padding: 28px 28px 28px calc(28px + 260px)", app_page)
        self.assertIn(".modal-backdrop { padding: 16px; }", app_page)
        self.assertIn(
            'self._route("/discover"',
            (PLUGIN_ROOT / "__init__.py").read_text(encoding="utf-8"),
        )
        self.assertTrue(
            (PLUGIN_ROOT / "dist/v1.0.21/assets/remoteEntry.js").is_file()
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

    def test_status_and_config_error_paths_use_safe_wrappers(self):
        plugin_source = (PLUGIN_ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("def _bridge_request(", plugin_source)
        self.assertIn('status, health = self._bridge_request("/health")', plugin_source)
        self.assertIn(
            'snapshot_status, snapshot = self._bridge_request("/v1/snapshot")',
            plugin_source,
        )
        self.assertIn('"code": "cleanup_bridge_not_ready"', plugin_source)

        config_source = (
            PLUGIN_ROOT / "src/components/Config.vue"
        ).read_text(encoding="utf-8")
        self.assertIn("function apiErrorMessage(err, fallback)", config_source)
        self.assertGreaterEqual(config_source.count("apiErrorMessage(err,"), 4)
        self.assertNotIn(
            "err?.message || payload?.error?.message",
            config_source,
        )

        config_bundle = (
            PLUGIN_ROOT
            / "dist/v1.0.21/assets/__federation_expose_Config-Brnz1KQ_.js"
        ).read_text(encoding="utf-8")
        self.assertIn("function apiErrorMessage(err, fallback)", config_bundle)
        self.assertGreaterEqual(config_bundle.count("apiErrorMessage(err,"), 4)

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
            'v-if="settingsOpen"',
            'class="modal settings-modal"',
            '清理台配置',
        ):
            self.assertIn(marker, teleported)
        self.assertIn("import Config from './Config.vue'", page)
        self.assertIn('aria-label="打开存储清理设置"', page)
        self.assertNotIn("{ id: 'brush'", source)
        self.assertNotIn("if (filter === 'brush')", source)
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
        self.assertIn("资源清单已过期，请点击“刷新资源清单”后再操作", page)
        self.assertIn("function executionErrorMessage(error, fallback)", page)
        self.assertIn("if (nested.plan) plan.value = nested.plan", page)
        self.assertIn("requestFailure.data = payload", page)
        self.assertIn("executeError.value = executionErrorMessage(err, '执行失败。')", page)
        self.assertIn('v-if="executeError && !executeResult"', page)
        self.assertIn(':disabled="executing || !inventoryCurrent || planExpired"', page)
        self.assertIn('class="plan-state blocked"', page)
        self.assertIn("v-if=\"!inventoryCurrent && !refreshing\"", page)
        self.assertIn(":disabled=\"!inventoryCurrent || refreshing\"", page)
        self.assertIn("FILTER_GROUPS", page)
        self.assertIn('class="filter-panel"', page)
        self.assertIn("同组条件单选；不同组条件按 AND 组合", page)
        self.assertIn("activeFilterChips", page)

    def test_moviepilot_page_guards_out_of_order_plan_responses(self):
        provider = (PLUGIN_ROOT / "src/provider.js").read_text(encoding="utf-8")
        wrapper = (
            PLUGIN_ROOT / "src/components/Page.vue"
        ).read_text(encoding="utf-8")
        self.assertIn("createLatestPlanApi", provider)
        self.assertIn("createLatestPlanApi", wrapper)
        self.assertIn('endsWith(\'/plan\')', provider)


if __name__ == "__main__":
    unittest.main()
