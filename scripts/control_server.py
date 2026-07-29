#!/opt/homebrew/bin/python3.12
"""Loopback-only control API for the PiNAS cleanup UI."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import threading
from typing import Any, Callable
from urllib.parse import urlparse

from action_planner import PlanInputError, build_plan, public_plan
from execution_engine import (
    ExecutionError,
    LocalExecutionRunner,
    LocalRecoveryRunner,
    SSHExecutionRunner,
    SSHRecoveryRunner,
    validate_confirmation,
)
from snapshot_integrity import validate_snapshot_pair


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY_BYTES = 64 * 1024
ALLOWED_ORIGINS = {
    "http://localhost:3000",
    "http://127.0.0.1:3000",
}


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


class ControlState:
    def __init__(
        self,
        *,
        project_root: Path,
        refresh_runner: Callable[[], None] | None = None,
        execution_runner: Callable[[dict[str, Any]], dict[str, Any]]
        | None = None,
        recovery_runner: Callable[..., dict[str, Any]] | None = None,
        local_nas: bool = False,
    ):
        self.project_root = project_root.resolve()
        self.public_snapshot_path = (
            self.project_root / "public/data/resource-snapshot.json"
        )
        self.private_inventory_path = (
            self.project_root / ".runtime/resource-inventory.json"
        )
        self.session_token_path = self.project_root / ".runtime/control-token"
        self.execution_audit_path = (
            self.project_root / ".runtime/execution-audit.jsonl"
        )
        self.session_token = secrets.token_urlsafe(32)
        self.plan_cache: dict[str, dict[str, Any]] = {}
        self.inventory_current = False
        self.operation_lock = threading.Lock()
        self.local_nas = local_nas
        self.runtime_mode = "pi-local" if local_nas else "ssh-client"
        self.host_name = socket.gethostname()
        self.refresh_runner = refresh_runner or self._run_collector
        self.execution_runner = execution_runner
        self.recovery_runner = recovery_runner or SSHRecoveryRunner()
        self.execution_enabled = execution_runner is not None
        self._write_session_token()
        self.inventory_current = self._inventory_pair_is_current()

    def _write_session_token(self) -> None:
        self.session_token_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.session_token_path.parent,
            prefix=".control-token.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(self.session_token + "\n")
            temporary = Path(handle.name)
        try:
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.session_token_path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _run_collector(self) -> None:
        command = [
            sys.executable,
            str(self.project_root / "scripts/collect-readonly-snapshot.py"),
        ]
        if self.local_nas:
            command.append("--local-nas")
        subprocess.run(
            command,
            cwd=self.project_root,
            check=True,
            timeout=600,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def snapshot(self) -> dict[str, Any]:
        return self._read_json(self.public_snapshot_path)

    def inventory(self) -> dict[str, Any]:
        return self._read_json(self.private_inventory_path)

    def _inventory_pair_is_current(self) -> bool:
        try:
            private_lstat = self.private_inventory_path.lstat()
            public_lstat = self.public_snapshot_path.lstat()
            if (
                stat.S_ISLNK(private_lstat.st_mode)
                or not stat.S_ISREG(private_lstat.st_mode)
                or private_lstat.st_mode & 0o077
                or stat.S_ISLNK(public_lstat.st_mode)
                or not stat.S_ISREG(public_lstat.st_mode)
            ):
                return False
            private = self.inventory()
            public = self.snapshot()
        except (OSError, json.JSONDecodeError):
            return False
        try:
            validate_snapshot_pair(public, private)
        except (TypeError, ValueError):
            return False
        return True

    def _accept_refreshed_inventory(self) -> dict[str, Any]:
        if not self._inventory_pair_is_current():
            self.inventory_current = False
            raise ApiError(
                502,
                "refresh_integrity_failed",
                "刷新结果未通过公开清单与私有库存一致性校验。",
            )
        self.inventory_current = True
        return self.snapshot()

    def _prune_plan_cache(self) -> None:
        now = datetime.now(timezone.utc)
        for plan_id, plan in list(self.plan_cache.items()):
            try:
                expires_at = datetime.fromisoformat(str(plan["expiresAt"]))
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
            except (KeyError, TypeError, ValueError):
                self.plan_cache.pop(plan_id, None)
                continue
            if expires_at.astimezone(timezone.utc) <= now:
                self.plan_cache.pop(plan_id, None)
        if len(self.plan_cache) > 128:
            oldest = sorted(
                self.plan_cache,
                key=lambda key: str(
                    self.plan_cache[key].get("createdAt") or ""
                ),
            )
            for plan_id in oldest[: len(self.plan_cache) - 128]:
                self.plan_cache.pop(plan_id, None)

    def build_public_plan(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.inventory_current:
            raise ApiError(
                409,
                "inventory_stale",
                "最新资源清单刷新失败，新操作保持锁定；请先刷新。",
            )
        snapshot_id = request.get("snapshotId")
        resource_ids = request.get("resourceIds")
        mode = request.get("mode")
        acknowledge_site_risk = request.get("acknowledgeSiteRisk", False)
        if not isinstance(snapshot_id, str):
            raise ApiError(400, "invalid_request", "snapshotId 格式不正确。")
        if (
            not isinstance(resource_ids, list)
            or not all(isinstance(item, str) for item in resource_ids)
        ):
            raise ApiError(400, "invalid_request", "resourceIds 格式不正确。")
        if not isinstance(mode, str):
            raise ApiError(400, "invalid_request", "mode 格式不正确。")
        if not isinstance(acknowledge_site_risk, bool):
            raise ApiError(
                400,
                "invalid_request",
                "acknowledgeSiteRisk 格式不正确。",
            )
        try:
            plan = build_plan(
                self.inventory(),
                snapshot_id=snapshot_id,
                resource_ids=resource_ids,
                mode=mode,
                acknowledge_site_risk=acknowledge_site_risk,
            )
        except PlanInputError as exc:
            raise ApiError(400, "invalid_request", str(exc)) from exc
        self.plan_cache[plan["planId"]] = plan
        self._prune_plan_cache()
        return public_plan(plan)

    def refresh(self) -> dict[str, Any]:
        if not self.operation_lock.acquire(blocking=False):
            raise ApiError(409, "refresh_in_progress", "正在刷新，请稍候。")
        try:
            self.refresh_runner()
            snapshot = self._accept_refreshed_inventory()
            self.plan_cache.clear()
            return snapshot
        except ApiError:
            self.inventory_current = False
            raise
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            self.inventory_current = False
            raise ApiError(502, "refresh_failed", "NAS 只读刷新失败。") from exc
        finally:
            self.operation_lock.release()

    def _unresolved_transaction_ids(self) -> list[str]:
        inventory = self.inventory()
        raw = inventory.get("unresolvedTransactionIds") or []
        if not isinstance(raw, list) or not all(
            isinstance(item, str) for item in raw
        ):
            raise ApiError(
                500,
                "invalid_recovery_inventory",
                "未完成事务清单格式不正确。",
            )
        if len(raw) > 100 or len(raw) != len(set(raw)):
            raise ApiError(
                500,
                "invalid_recovery_inventory",
                "未完成事务清单无法验证。",
            )
        expected = int(
            (inventory.get("stats") or {}).get(
                "unresolvedTransactions"
            )
            or 0
        )
        if len(raw) != expected:
            raise ApiError(
                409,
                "recovery_inventory_stale",
                "未完成事务清单需要刷新。",
            )
        return raw

    def recovery_status(self) -> list[dict[str, Any]]:
        if not self.operation_lock.acquire(blocking=False):
            raise ApiError(409, "operation_in_progress", "另一项操作正在进行。")
        try:
            results = []
            for plan_id in self._unresolved_transaction_ids():
                try:
                    result = self.recovery_runner(
                        plan_id=plan_id,
                        action="inspect",
                        confirm_phrase="",
                    )
                except ExecutionError as exc:
                    raise ApiError(502, exc.code, exc.message) from exc
                results.append(result)
            return results
        finally:
            self.operation_lock.release()

    def protection_gaps(self) -> list[dict[str, Any]]:
        inventory = self.inventory()
        raw = inventory.get("hrMissingRecords") or []
        if not isinstance(raw, list) or len(raw) > 100:
            raise ApiError(
                500,
                "invalid_hr_gap_inventory",
                "H&R 缺口清单格式不正确。",
            )
        result = []
        for item in raw:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("title"), str)
                or not isinstance(item.get("coveredByCandidate"), bool)
                or (
                    item.get("linkedResourceTitle") is not None
                    and not isinstance(item.get("linkedResourceTitle"), str)
                )
            ):
                raise ApiError(
                    500,
                    "invalid_hr_gap_inventory",
                    "H&R 缺口清单无法验证。",
                )
            public_item = {
                "title": item["title"],
                "coveredByCandidate": item["coveredByCandidate"],
            }
            linked_title = str(item.get("linkedResourceTitle") or "").strip()
            if linked_title:
                public_item["linkedResourceTitle"] = linked_title[:300]
            result.append(public_item)
        expected = int(
            (inventory.get("stats") or {}).get("hrMissingQbTasks")
            or 0
        )
        if len(result) != expected:
            raise ApiError(
                409,
                "hr_gap_inventory_stale",
                "H&R 缺口清单需要刷新。",
            )
        return result

    def _append_execution_audit(
        self,
        plan: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        record = {
            "recordedAt": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            "planId": plan["planId"],
            "snapshotId": plan["snapshotId"],
            "mode": plan["mode"],
            "resourceIds": sorted(
                item["id"] for item in plan.get("resources") or []
            ),
            "operationCounts": {
                key: len(value)
                for key, value in plan["operations"].items()
            },
            "result": result,
        }
        self.execution_audit_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.execution_audit_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.write(
                descriptor,
                (
                    json.dumps(record, ensure_ascii=False, sort_keys=True)
                    + "\n"
                ).encode("utf-8"),
            )
        finally:
            os.close(descriptor)

    def _append_recovery_audit(
        self,
        *,
        plan_id: str,
        action: str,
        result: dict[str, Any],
    ) -> None:
        record = {
            "recordedAt": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            "kind": "recovery",
            "planId": plan_id,
            "action": action,
            "result": result,
        }
        self.execution_audit_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.execution_audit_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.write(
                descriptor,
                (
                    json.dumps(record, ensure_ascii=False, sort_keys=True)
                    + "\n"
                ).encode("utf-8"),
            )
        finally:
            os.close(descriptor)

    def recover(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.execution_enabled:
            raise ApiError(501, "execution_disabled", "执行引擎尚未启用。")
        plan_id = request.get("planId")
        action = request.get("action")
        confirm_phrase = request.get("confirmPhrase")
        if (
            not isinstance(plan_id, str)
            or action not in {"rollback", "finalize"}
            or not isinstance(confirm_phrase, str)
        ):
            raise ApiError(400, "invalid_request", "事务恢复请求格式不正确。")
        if not self.operation_lock.acquire(blocking=False):
            raise ApiError(409, "operation_in_progress", "另一项操作正在进行。")
        try:
            if plan_id not in self._unresolved_transaction_ids():
                raise ApiError(
                    409,
                    "recovery_not_current",
                    "该事务不在当前未完成清单中，请刷新。",
                )
            self.inventory_current = False
            try:
                result = self.recovery_runner(
                    plan_id=plan_id,
                    action=action,
                    confirm_phrase=confirm_phrase,
                )
            except ExecutionError as exc:
                failure = {
                    "ok": False,
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                    },
                }
                self._append_recovery_audit(
                    plan_id=plan_id,
                    action=action,
                    result=failure,
                )
                raise ApiError(502, exc.code, exc.message) from exc
            self._append_recovery_audit(
                plan_id=plan_id,
                action=action,
                result=result,
            )
            snapshot_refresh_pending = False
            try:
                self.refresh_runner()
                self._accept_refreshed_inventory()
            except ApiError:
                snapshot_refresh_pending = True
            except (
                OSError,
                subprocess.SubprocessError,
                json.JSONDecodeError,
            ):
                snapshot_refresh_pending = True
            self.plan_cache.clear()
            return {
                **result,
                "snapshotRefreshPending": snapshot_refresh_pending,
            }
        finally:
            self.operation_lock.release()

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.execution_enabled or self.execution_runner is None:
            raise ApiError(501, "execution_disabled", "执行引擎尚未启用。")
        plan_id = request.get("planId")
        confirm_phrase = request.get("confirmPhrase")
        if not isinstance(plan_id, str) or not isinstance(
            confirm_phrase, str
        ):
            raise ApiError(400, "invalid_request", "执行确认格式不正确。")
        plan = self.plan_cache.get(plan_id)
        if plan is None:
            raise ApiError(
                409,
                "plan_not_cached",
                "安全预演不存在或控制服务已经重启，请重新生成。",
            )
        try:
            validate_confirmation(plan, confirm_phrase=confirm_phrase)
        except ExecutionError as exc:
            raise ApiError(409, exc.code, exc.message) from exc
        if not self.operation_lock.acquire(blocking=False):
            raise ApiError(409, "operation_in_progress", "另一项操作正在进行。")
        try:
            try:
                self.refresh_runner()
                self._accept_refreshed_inventory()
                latest_inventory = self.inventory()
            except ApiError as exc:
                self.inventory_current = False
                raise ApiError(
                    502,
                    "preflight_refresh_failed",
                    "执行前的资源清单未通过一致性校验，未开始任何改动。",
                ) from exc
            except (
                OSError,
                subprocess.SubprocessError,
                json.JSONDecodeError,
            ) as exc:
                self.inventory_current = False
                raise ApiError(
                    502,
                    "preflight_refresh_failed",
                    "执行前无法刷新 NAS 状态，未开始任何改动。",
                ) from exc
            try:
                validate_confirmation(
                    plan,
                    confirm_phrase=confirm_phrase,
                )
            except ExecutionError as exc:
                raise ApiError(409, exc.code, exc.message) from exc
            try:
                rebuilt = build_plan(
                    latest_inventory,
                    snapshot_id=str(latest_inventory.get("snapshotId") or ""),
                    resource_ids=[
                        item["id"] for item in plan.get("resources") or []
                    ],
                    mode=plan["mode"],
                    acknowledge_site_risk=plan[
                        "acknowledgeSiteRisk"
                    ],
                )
                validate_confirmation(
                    rebuilt,
                    confirm_phrase=confirm_phrase,
                )
            except (PlanInputError, ExecutionError) as exc:
                code = getattr(exc, "code", "plan_rebuild_failed")
                raise ApiError(
                    409,
                    code,
                    str(exc),
                    details={"plan": public_plan(rebuilt)},
                ) from exc
            if rebuilt["planId"] != plan["planId"]:
                raise ApiError(
                    409,
                    "plan_changed",
                    "所选资源的任务或文件状态已变化，请重新确认。",
                    details={"plan": public_plan(rebuilt)},
                )
            self.inventory_current = False
            try:
                result = self.execution_runner(rebuilt)
            except ExecutionError as exc:
                self._append_execution_audit(
                    rebuilt,
                    {
                        "ok": False,
                        "error": {
                            "code": exc.code,
                            "message": exc.message,
                        },
                    },
                )
                raise ApiError(502, exc.code, exc.message) from exc
            self._append_execution_audit(rebuilt, result)
            snapshot_refresh_pending = False
            try:
                self.refresh_runner()
                self._accept_refreshed_inventory()
            except ApiError:
                snapshot_refresh_pending = True
            except (
                OSError,
                subprocess.SubprocessError,
                json.JSONDecodeError,
            ):
                snapshot_refresh_pending = True
            self.plan_cache.clear()
            return {
                **result,
                "snapshotRefreshPending": snapshot_refresh_pending,
            }
        finally:
            self.operation_lock.release()


def handler_class(state: ControlState):
    class ControlHandler(BaseHTTPRequestHandler):
        server_version = "PiNASCleanup/1"
        sys_version = ""

        def log_message(self, format_string: str, *args: Any) -> None:
            message = format_string % args
            print(f"[control] {self.client_address[0]} {message}")

        def _origin(self) -> str:
            return self.headers.get("Origin", "")

        def _require_allowed_origin(self) -> str:
            origin = self._origin()
            if origin not in ALLOWED_ORIGINS:
                raise ApiError(403, "origin_denied", "请求来源未获允许。")
            return origin

        def _require_session(self) -> None:
            self._require_allowed_origin()
            token = self.headers.get("X-PiNAS-Session", "")
            if not secrets.compare_digest(token, state.session_token):
                raise ApiError(403, "session_denied", "本地会话已失效，请刷新页面。")

        def _read_body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ApiError(400, "invalid_length", "请求长度无效。") from exc
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ApiError(400, "invalid_length", "请求内容为空或过大。")
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ApiError(400, "invalid_json", "请求不是有效 JSON。") from exc
            if not isinstance(payload, dict):
                raise ApiError(400, "invalid_json", "请求必须是 JSON 对象。")
            return payload

        def _send_json(
            self,
            status: int,
            payload: dict[str, Any],
            *,
            origin: str | None = None,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if origin in ALLOWED_ORIGINS:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # A browser/gateway may time out or navigate away while a
                # long-running qB operation is finishing. The operation's
                # transaction is already audited; do not turn the closed
                # response socket into a second server error.
                return

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, ApiError):
                status, code, message = exc.status, exc.code, exc.message
            else:
                status, code, message = (
                    500,
                    "internal_error",
                    "本地控制服务发生错误。",
                )
            self._send_json(
                status,
                {
                    "ok": False,
                    "error": {
                        "code": code,
                        "message": message,
                        **getattr(exc, "details", {}),
                    },
                },
                origin=self._origin(),
            )

        def do_OPTIONS(self) -> None:
            try:
                origin = self._require_allowed_origin()
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header(
                    "Access-Control-Allow-Headers",
                    "Content-Type, X-PiNAS-Session",
                )
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Max-Age", "600")
                self.send_header("Vary", "Origin")
                self.end_headers()
            except Exception as exc:
                self._handle_error(exc)

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                if path == "/health":
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "service": "PiNAS Cleanup Control",
                            "executionEnabled": state.execution_enabled,
                            "inventoryCurrent": state.inventory_current,
                            "runtimeMode": state.runtime_mode,
                            "hostName": state.host_name,
                        },
                        origin=self._origin(),
                    )
                    return
                if path == "/v1/snapshot":
                    self._send_json(
                        200,
                        {"ok": True, "snapshot": state.snapshot()},
                        origin=self._origin(),
                    )
                    return
                if path == "/v1/session":
                    origin = self._require_allowed_origin()
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "sessionToken": state.session_token,
                            "executionEnabled": state.execution_enabled,
                            "inventoryCurrent": state.inventory_current,
                            "runtimeMode": state.runtime_mode,
                            "hostName": state.host_name,
                        },
                        origin=origin,
                    )
                    return
                if path == "/v1/recovery":
                    self._require_session()
                    recoveries = state.recovery_status()
                    self._send_json(
                        200,
                        {"ok": True, "recoveries": recoveries},
                        origin=self._origin(),
                    )
                    return
                if path == "/v1/protection-gaps":
                    self._require_session()
                    gaps = state.protection_gaps()
                    self._send_json(
                        200,
                        {"ok": True, "gaps": gaps},
                        origin=self._origin(),
                    )
                    return
                raise ApiError(404, "not_found", "接口不存在。")
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            try:
                self._require_session()
                path = urlparse(self.path).path
                if path == "/v1/plan":
                    plan = state.build_public_plan(self._read_body())
                    self._send_json(
                        200,
                        {"ok": True, "plan": plan},
                        origin=self._origin(),
                    )
                    return
                if path == "/v1/refresh":
                    self._read_body()
                    snapshot = state.refresh()
                    self._send_json(
                        200,
                        {"ok": True, "snapshot": snapshot},
                        origin=self._origin(),
                    )
                    return
                if path == "/v1/execute":
                    result = state.execute(self._read_body())
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "result": result},
                        origin=self._origin(),
                    )
                    return
                if path == "/v1/recovery":
                    result = state.recover(self._read_body())
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "result": result},
                        origin=self._origin(),
                    )
                    return
                raise ApiError(404, "not_found", "接口不存在。")
            except Exception as exc:
                self._handle_error(exc)

    return ControlHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument(
        "--enable-execution",
        action="store_true",
        help="Enable confirmation-gated NAS mutations.",
    )
    parser.add_argument(
        "--ssh-host",
        default="nas-user@192.0.2.1",
    )
    parser.add_argument(
        "--local-nas",
        action="store_true",
        help="Collect and execute directly on the Pi.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("control server must bind to loopback")
    state = ControlState(
        project_root=args.project_root,
        local_nas=args.local_nas,
        execution_runner=(
            (
                LocalExecutionRunner()
                if args.local_nas
                else SSHExecutionRunner(host=args.ssh_host)
            )
            if args.enable_execution
            else None
        ),
        recovery_runner=(
            LocalRecoveryRunner()
            if args.local_nas
            else SSHRecoveryRunner(host=args.ssh_host)
        ),
    )
    server = ThreadingHTTPServer((args.host, args.port), handler_class(state))
    print(f"PiNAS cleanup control listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
