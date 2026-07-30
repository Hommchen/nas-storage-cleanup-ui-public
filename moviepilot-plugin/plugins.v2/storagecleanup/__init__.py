"""MoviePilot native UI for the PiNAS storage cleanup console."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from fastapi import Body
from fastapi.responses import JSONResponse

from app.plugins import _PluginBase

from .bridge_client import CleanupBridge


class StorageCleanup(_PluginBase):
    plugin_name = "存储清理"
    plugin_desc = "看清媒体、做种、H&R 与硬链接关系后再清理。"
    plugin_icon = "mdi-broom"
    plugin_version = "1.0.3"
    plugin_author = "Hommchen"
    author_url = "https://github.com/Hommchen/pinas-storage-cleanup-ui"
    plugin_config_prefix = "storagecleanup_"
    plugin_order = 34
    auth_level = 1

    def init_plugin(self, config: dict | None = None) -> None:
        self._bridge = CleanupBridge()

    @staticmethod
    def get_state() -> bool:
        return True

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    @staticmethod
    def get_render_mode() -> Tuple[str, str]:
        return "vue", "dist/v1.0.3/assets"

    @staticmethod
    def get_form() -> Tuple[List[dict], Dict[str, Any]]:
        return [], {}

    @staticmethod
    def get_page() -> List[dict]:
        return []

    @staticmethod
    def get_sidebar_nav() -> List[Dict[str, Any]]:
        return [
            {
                "nav_key": "main",
                "title": "存储清理",
                "icon": "mdi-broom",
                "section": "organize",
                "permission": "manage",
                "order": 34,
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            self._route("/status", self.status, ["GET"], "读取清理台状态"),
            self._route("/snapshot", self.snapshot, ["GET"], "读取资源快照"),
            self._route("/refresh", self.refresh, ["POST"], "刷新资源快照"),
            self._route("/plan", self.plan, ["POST"], "生成清理计划"),
            self._route("/execute", self.execute, ["POST"], "执行清理计划"),
            self._route("/recovery", self.recovery_status, ["GET"], "读取恢复状态"),
            self._route("/recovery", self.recovery, ["POST"], "执行事务恢复"),
            self._route(
                "/protection-gaps",
                self.protection_gaps,
                ["GET"],
                "读取 H&R 缺口",
            ),
        ]

    @staticmethod
    def _route(
        path: str,
        endpoint: Any,
        methods: List[str],
        summary: str,
    ) -> Dict[str, Any]:
        return {
            "path": path,
            "endpoint": endpoint,
            "methods": methods,
            "auth": "bear",
            "summary": summary,
        }

    def _proxy(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> JSONResponse:
        try:
            status, content = self._bridge.request(
                path,
                method=method,
                payload=payload,
            )
        except RuntimeError as exc:
            status, content = 503, {
                "ok": False,
                "error": {
                    "code": "cleanup_bridge_not_ready",
                    "message": str(exc),
                },
            }
        return JSONResponse(status_code=status, content=content)

    def status(self) -> JSONResponse:
        status, health = self._bridge.request("/health")
        if status != 200 or not health.get("ok"):
            return JSONResponse(status_code=status, content=health)
        snapshot_status, snapshot = self._bridge.request("/v1/snapshot")
        if snapshot_status != 200:
            return JSONResponse(status_code=snapshot_status, content=snapshot)
        return JSONResponse(
            content={
                "ok": True,
                "health": health,
                "snapshot": snapshot.get("snapshot"),
            }
        )

    def snapshot(self) -> JSONResponse:
        return self._proxy("/v1/snapshot")

    def refresh(self, payload: dict = Body(default={})) -> JSONResponse:
        return self._proxy("/v1/refresh", method="POST", payload=payload)

    def plan(self, payload: dict = Body(...)) -> JSONResponse:
        return self._proxy("/v1/plan", method="POST", payload=payload)

    def execute(self, payload: dict = Body(...)) -> JSONResponse:
        return self._proxy("/v1/execute", method="POST", payload=payload)

    def recovery_status(self) -> JSONResponse:
        return self._proxy("/v1/recovery")

    def recovery(self, payload: dict = Body(...)) -> JSONResponse:
        return self._proxy("/v1/recovery", method="POST", payload=payload)

    def protection_gaps(self) -> JSONResponse:
        return self._proxy("/v1/protection-gaps")

    @staticmethod
    def stop_service() -> None:
        return None
