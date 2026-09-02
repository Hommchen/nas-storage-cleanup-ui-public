#!/opt/homebrew/bin/python3.12

from __future__ import annotations

from datetime import datetime, timezone
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from control_server import ApiError, ControlState, handler_class
from configuration import default_config


ORIGIN = "http://localhost:3000"
TASK_HASH = "a" * 40
SNAPSHOT_ID = "snap_" + "f" * 24


def fixture_inventory():
    resource = {
        "id": "res_fixture",
        "title": "示例",
        "englishTitle": "Example",
        "edition": "电影",
        "sizeGiB": 1,
        "library": True,
        "hr": False,
        "brush": False,
        "protected": False,
        "metadataVerified": True,
        "identity": "movie:fixture",
        "allLinksKnown": True,
        "files": [
            {
                "path": "/mnt/sdc/downloads/completed/Example/Example.mkv",
                "dev": 1,
                "inode": 2,
                "size": 1024,
                "nlink": 1,
                "allowed": True,
            }
        ],
        "cleanupFiles": [
            {
                "path": "/mnt/sdc/downloads/completed/Example/Example.mkv",
                "source": "library",
                "allowed": True,
                "exists": True,
                "regular": True,
                "relativeSafe": True,
                "required": True,
                "dev": 1,
                "inode": 2,
                "size": 1024,
                "nlink": 1,
            }
        ],
        "cleanupLinksKnown": True,
        "libraryScanVerified": True,
        "qbFileListsVerified": True,
        "roots": [
            {
                "path": "/mnt/sdc/downloads/completed/Example",
                "allowed": True,
            }
        ],
        "qbTasks": [
            {
                "hash": TASK_HASH,
                "name": "Example.Release.2026",
                "site": "公开 BT",
                "scope": "整部",
                "state": "stalledUP",
                "progress": 1,
                "private": False,
                "hr": False,
                "hrUnknown": False,
                "selfPublish": False,
                "contentPath": "/mnt/sdc/downloads/completed/Example/Example.mkv",
                "savePath": "/mnt/sdc/downloads/completed",
                "category": "",
                "tags": "",
                "fileListVerified": True,
                "exactFiles": [],
            }
        ],
    }
    return {
        "schemaVersion": 2,
        "snapshotId": SNAPSHOT_ID,
        "generatedAt": "2026-07-29T00:00:00+08:00",
        "stats": {
            "resources": 1,
            "hrSourceAvailable": True,
            "hrActiveTitles": 0,
            "hrMatchedQbTasks": 0,
            "hrMissingQbTasks": 0,
            "hrMissingUncovered": 0,
            "hrMissingUnassigned": 0,
            "unresolvedTransactions": 0,
            "metadataUnverifiedResources": 0,
        },
        "unresolvedTransactionIds": [],
        "hrMissingRecords": [],
        "resources": {"res_fixture": resource},
    }


def write_inventory_pair(root: Path, inventory: dict) -> None:
    private_path = root / ".runtime/resource-inventory.json"
    public_path = root / "public/data/resource-snapshot.json"
    private_path.write_text(json.dumps(inventory), encoding="utf-8")
    private_path.chmod(0o600)
    public_path.write_text(
        json.dumps(
            {
                "schemaVersion": inventory["schemaVersion"],
                "snapshotId": inventory["snapshotId"],
                "generatedAt": inventory["generatedAt"],
                "stats": inventory["stats"],
                "resources": [
                    {
                        "id": item["id"],
                        "title": item["title"],
                        "englishTitle": item["englishTitle"],
                        "metadataVerified": item["metadataVerified"],
                        "protected": item["protected"],
                        "seedTasks": [],
                    }
                    for item in inventory["resources"].values()
                ],
            }
        ),
        encoding="utf-8",
    )


class ControlServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        (cls.root / "public/data").mkdir(parents=True)
        (cls.root / ".runtime").mkdir(parents=True)
        inventory = fixture_inventory()
        write_inventory_pair(cls.root, inventory)
        cls.refresh_count = 0

        def fake_refresh():
            cls.refresh_count += 1

        cls.state = ControlState(
            project_root=cls.root,
            refresh_runner=fake_refresh,
        )
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            handler_class(cls.state),
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temporary.cleanup()

    def request(self, method, path, payload=None, *, origin=None, token=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        body = None
        if origin:
            headers["Origin"] = origin
        if token:
            headers["X-PiNAS-Session"] = token
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        parsed = json.loads(raw) if raw else {}
        response_headers = dict(response.getheaders())
        connection.close()
        return response.status, response_headers, parsed

    def test_health_is_read_only_and_execution_is_disabled(self):
        status, _, payload = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["executionEnabled"])
        self.assertTrue(payload["inventoryCurrent"])
        self.assertTrue(payload["snapshotFresh"])
        self.assertEqual(payload["runtimeMode"], "ssh-client")

    def test_configured_snapshot_expiry_locks_new_plans(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "public/data").mkdir(parents=True)
            (root / ".runtime").mkdir(parents=True)
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            inventory = fixture_inventory()
            inventory["generatedAt"] = "2026-08-30T10:00:00+00:00"
            write_inventory_pair(root, inventory)
            now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

            with patch(
                "control_server.probe_config",
                return_value={"ok": True},
            ):
                state = ControlState(
                    project_root=root,
                    config_path=config_path,
                    config={**default_config(), "snapshot_max_age_seconds": 3600},
                    refresh_runner=lambda: None,
                    clock=lambda: now,
                )

            self.assertFalse(state.snapshot_fresh)
            self.assertEqual(state.freshness_status()["snapshotAgeSeconds"], 7200)
            with self.assertRaises(ApiError) as context:
                state.build_public_plan(
                    {
                        "snapshotId": SNAPSHOT_ID,
                        "resourceIds": ["res_fixture"],
                        "mode": "pause",
                    }
                )
            self.assertEqual(context.exception.code, "inventory_stale")
            self.assertIn("超过有效期", context.exception.message)

    def test_local_nas_runtime_is_reported(self):
        state = ControlState(
            project_root=self.root,
            refresh_runner=lambda: None,
            local_nas=True,
        )
        self.assertEqual(state.runtime_mode, "pi-local")

    def test_discovery_is_session_gated_and_read_only(self):
        denied, _, _ = self.request("GET", "/v1/discover")
        with patch.object(
            self.state,
            "discover",
            return_value={"readOnly": True, "checks": [], "ready": False},
        ):
            allowed, _, payload = self.request(
                "GET",
                "/v1/discover",
                origin=ORIGIN,
                token=self.state.session_token,
            )
        self.assertEqual(denied, 403)
        self.assertEqual(allowed, 200)
        self.assertTrue(payload["readOnly"])
        self.assertIn("checks", payload)

    def test_config_update_is_persisted_and_invalidates_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "public/data").mkdir(parents=True)
            (root / ".runtime").mkdir(parents=True)
            write_inventory_pair(root, fixture_inventory())
            state = ControlState(
                project_root=root,
                refresh_runner=lambda: None,
            )
            result = state.update_config({"config": state.config})
            self.assertFalse(result["probe"]["ok"])
            self.assertFalse(state.inventory_current)
            self.assertEqual(
                json.loads((root / ".runtime/config.json").read_text())["version"],
                1,
            )

    def test_startup_locks_mismatched_or_publicly_sensitive_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "public/data").mkdir(parents=True)
            (root / ".runtime").mkdir(parents=True)
            inventory = fixture_inventory()
            write_inventory_pair(root, inventory)
            public_path = root / "public/data/resource-snapshot.json"
            public = json.loads(public_path.read_text(encoding="utf-8"))
            public["snapshotId"] = "snap_other"
            public["leak"] = "/mnt/sdc/private"
            public_path.write_text(json.dumps(public), encoding="utf-8")

            state = ControlState(
                project_root=root,
                refresh_runner=lambda: None,
            )

            self.assertFalse(state.inventory_current)
            with self.assertRaises(ApiError) as context:
                state.build_public_plan(
                    {
                        "snapshotId": SNAPSHOT_ID,
                        "resourceIds": ["res_fixture"],
                        "mode": "pause",
                    }
                )
            self.assertEqual(context.exception.code, "inventory_stale")

    def test_successful_refresh_must_produce_a_matching_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "public/data").mkdir(parents=True)
            (root / ".runtime").mkdir(parents=True)
            write_inventory_pair(root, fixture_inventory())
            state = ControlState(
                project_root=root,
                refresh_runner=lambda: (
                    root / "public/data/resource-snapshot.json"
                ).write_text("{}", encoding="utf-8"),
            )

            with self.assertRaises(ApiError) as context:
                state.refresh()

            self.assertEqual(
                context.exception.code,
                "refresh_integrity_failed",
            )
            self.assertFalse(state.inventory_current)

    def test_session_requires_exact_allowed_origin(self):
        denied, _, _ = self.request(
            "GET",
            "/v1/session",
            origin="https://malicious.example",
        )
        allowed, headers, payload = self.request(
            "GET",
            "/v1/session",
            origin=ORIGIN,
        )
        self.assertEqual(denied, 403)
        self.assertEqual(allowed, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], ORIGIN)
        self.assertEqual(payload["sessionToken"], self.state.session_token)

    def test_plan_requires_session_and_strips_sensitive_operations(self):
        request = {
            "snapshotId": SNAPSHOT_ID,
            "resourceIds": ["res_fixture"],
            "mode": "pause",
            "acknowledgeSiteRisk": False,
        }
        denied, _, _ = self.request(
            "POST",
            "/v1/plan",
            request,
            origin=ORIGIN,
        )
        allowed, _, payload = self.request(
            "POST",
            "/v1/plan",
            request,
            origin=ORIGIN,
            token=self.state.session_token,
        )
        serialized = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(denied, 403)
        self.assertEqual(allowed, 200)
        self.assertTrue(payload["plan"]["canExecute"])
        self.assertEqual(payload["plan"]["operationCounts"]["qbStop"], 1)
        self.assertNotIn(TASK_HASH, serialized)
        self.assertNotIn("/mnt/", serialized)

    def test_stale_snapshot_returns_a_blocked_plan(self):
        status, _, payload = self.request(
            "POST",
            "/v1/plan",
            {
                "snapshotId": "snap_old",
                "resourceIds": ["res_fixture"],
                "mode": "pause",
            },
            origin=ORIGIN,
            token=self.state.session_token,
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["plan"]["canExecute"])
        self.assertIn(
            "stale_snapshot",
            {item["code"] for item in payload["plan"]["blocks"]},
        )

    def test_refresh_is_session_gated(self):
        before = self.refresh_count
        status, _, payload = self.request(
            "POST",
            "/v1/refresh",
            {},
            origin=ORIGIN,
            token=self.state.session_token,
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.refresh_count, before + 1)

    def test_failed_refresh_locks_new_plans_until_a_successful_refresh(self):
        state = ControlState(
            project_root=self.root,
            refresh_runner=lambda: (_ for _ in ()).throw(OSError("offline")),
        )
        with self.assertRaises(ApiError) as refresh_error:
            state.refresh()
        self.assertEqual(refresh_error.exception.code, "refresh_failed")
        self.assertFalse(state.inventory_current)

        with self.assertRaises(ApiError) as plan_error:
            state.build_public_plan(
                {
                    "snapshotId": SNAPSHOT_ID,
                    "resourceIds": ["res_fixture"],
                    "mode": "pause",
                }
            )
        self.assertEqual(plan_error.exception.code, "inventory_stale")

        state.refresh_runner = lambda: None
        state.refresh()
        self.assertTrue(state.inventory_current)
        self.assertTrue(
            state.build_public_plan(
                {
                    "snapshotId": SNAPSHOT_ID,
                    "resourceIds": ["res_fixture"],
                    "mode": "pause",
                }
            )["canExecute"]
        )

    def test_execute_is_explicitly_disabled(self):
        status, _, payload = self.request(
            "POST",
            "/v1/execute",
            {},
            origin=ORIGIN,
            token=self.state.session_token,
        )
        self.assertEqual(status, 501)
        self.assertEqual(payload["error"]["code"], "execution_disabled")

    def test_recovery_inspection_requires_session(self):
        denied, _, _ = self.request(
            "GET",
            "/v1/recovery",
            origin=ORIGIN,
        )
        allowed, _, payload = self.request(
            "GET",
            "/v1/recovery",
            origin=ORIGIN,
            token=self.state.session_token,
        )

        self.assertEqual(denied, 403)
        self.assertEqual(allowed, 200)
        self.assertEqual(payload["recoveries"], [])

    def test_recovery_mutation_is_explicitly_disabled(self):
        status, _, payload = self.request(
            "POST",
            "/v1/recovery",
            {
                "planId": "plan_" + "c" * 24,
                "action": "rollback",
                "confirmPhrase": "回滚事务 plan_" + "c" * 24,
            },
            origin=ORIGIN,
            token=self.state.session_token,
        )
        self.assertEqual(status, 501)
        self.assertEqual(payload["error"]["code"], "execution_disabled")

    def test_hr_gap_details_are_session_only_and_sanitized(self):
        inventory_path = self.root / ".runtime/resource-inventory.json"
        inventory = fixture_inventory()
        inventory["stats"]["hrMissingQbTasks"] = 1
        inventory["stats"]["hrMissingUncovered"] = 1
        inventory["hrMissingRecords"] = [
            {
                "id": "private-site-id",
                "title": "Sugar S02E06 1080p",
                "coveredByCandidate": False,
                "qbTaskPresent": True,
                "linkedResourceTitle": "谜探休格",
            }
        ]
        write_inventory_pair(self.root, inventory)
        try:
            denied, _, _ = self.request(
                "GET",
                "/v1/protection-gaps",
                origin=ORIGIN,
            )
            allowed, _, payload = self.request(
                "GET",
                "/v1/protection-gaps",
                origin=ORIGIN,
                token=self.state.session_token,
            )
        finally:
            write_inventory_pair(self.root, fixture_inventory())

        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(denied, 403)
        self.assertEqual(allowed, 200)
        self.assertEqual(payload["gaps"][0]["title"], "Sugar S02E06 1080p")
        self.assertEqual(
            payload["gaps"][0]["linkedResourceTitle"],
            "谜探休格",
        )
        self.assertTrue(payload["gaps"][0]["qbTaskPresent"])
        self.assertNotIn("private-site-id", serialized)
        self.assertNotIn(TASK_HASH, serialized)
        self.assertNotIn("/mnt/", serialized)

    def test_enabled_recovery_is_current_confirmed_and_audited(self):
        inventory_path = self.root / ".runtime/resource-inventory.json"
        unresolved = fixture_inventory()
        plan_id = "plan_" + "c" * 24
        unresolved["stats"]["unresolvedTransactions"] = 1
        unresolved["unresolvedTransactionIds"] = [plan_id]
        write_inventory_pair(self.root, unresolved)
        calls = []

        def fake_recovery(**kwargs):
            calls.append(kwargs)
            if kwargs["action"] == "inspect":
                return {
                    "ok": True,
                    "planId": plan_id,
                    "phase": "uncertain",
                    "mode": "delete",
                    "taskCount": 1,
                    "tasksPresent": 1,
                    "tasksAbsent": 0,
                    "filesAtSource": 0,
                    "filesQuarantined": 2,
                    "filesAlreadyGone": 0,
                    "rollbackPhrase": f"回滚事务 {plan_id}",
                    "finalizePhrase": f"完成事务 {plan_id}",
                }
            return {
                "ok": True,
                "planId": plan_id,
                "phase": "rolled_back",
                "resolved": True,
            }

        state = ControlState(
            project_root=self.root,
            refresh_runner=lambda: None,
            execution_runner=lambda plan: {},
            recovery_runner=fake_recovery,
        )
        try:
            status = state.recovery_status()
            result = state.recover(
                {
                    "planId": plan_id,
                    "action": "rollback",
                    "confirmPhrase": f"回滚事务 {plan_id}",
                }
            )
        finally:
            write_inventory_pair(self.root, fixture_inventory())

        self.assertEqual(status[0]["phase"], "uncertain")
        self.assertEqual(result["phase"], "rolled_back")
        self.assertEqual(
            [item["action"] for item in calls],
            ["inspect", "rollback"],
        )
        audit = self.root / ".runtime/execution-audit.jsonl"
        records = [
            json.loads(line)
            for line in audit.read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(
            any(
                item.get("kind") == "recovery"
                and item.get("planId") == plan_id
                for item in records
            )
        )

    def test_execution_ignores_unrelated_global_snapshot_changes(self):
        inventory_path = self.root / ".runtime/resource-inventory.json"
        write_inventory_pair(self.root, fixture_inventory())
        refresh_count = 0
        executions = []

        def changing_refresh():
            nonlocal refresh_count
            refresh_count += 1
            if refresh_count == 1:
                changed = fixture_inventory()
                changed["snapshotId"] = "snap_" + "e" * 24
                unrelated = fixture_inventory()["resources"]["res_fixture"]
                unrelated = json.loads(json.dumps(unrelated))
                unrelated["id"] = "res_unrelated"
                unrelated["identity"] = "movie:unrelated"
                changed["resources"]["res_unrelated"] = unrelated
                changed["stats"]["resources"] = 2
                write_inventory_pair(self.root, changed)

        def fake_execution(plan):
            executions.append(plan["planId"])
            return {
                "ok": True,
                "planId": plan["planId"],
                "mode": plan["mode"],
                "qbStopped": 1,
                "qbRemoved": 0,
                "filesDeleted": 0,
                "backupCreated": False,
            }

        state = ControlState(
            project_root=self.root,
            refresh_runner=changing_refresh,
            execution_runner=fake_execution,
        )
        public = state.build_public_plan(
            {
                "snapshotId": SNAPSHOT_ID,
                "resourceIds": ["res_fixture"],
                "mode": "pause",
                "acknowledgeSiteRisk": False,
            }
        )
        try:
            result = state.execute(
                {
                    "planId": public["planId"],
                    "confirmPhrase": public["confirmPhrase"],
                }
            )
        finally:
            write_inventory_pair(self.root, fixture_inventory())

        self.assertTrue(result["ok"])
        self.assertEqual(executions, [public["planId"]])

    def test_execution_rejects_selected_resource_state_change(self):
        inventory_path = self.root / ".runtime/resource-inventory.json"
        write_inventory_pair(self.root, fixture_inventory())
        executions = []

        def changing_refresh():
            changed = fixture_inventory()
            changed["snapshotId"] = "snap_" + "d" * 24
            changed["resources"]["res_fixture"]["qbTasks"][0]["hash"] = (
                "c" * 40
            )
            write_inventory_pair(self.root, changed)

        state = ControlState(
            project_root=self.root,
            refresh_runner=changing_refresh,
            execution_runner=lambda plan: executions.append(plan["planId"]),
        )
        public = state.build_public_plan(
            {
                "snapshotId": SNAPSHOT_ID,
                "resourceIds": ["res_fixture"],
                "mode": "pause",
                "acknowledgeSiteRisk": False,
            }
        )
        try:
            with self.assertRaises(ApiError) as context:
                state.execute(
                    {
                        "planId": public["planId"],
                        "confirmPhrase": public["confirmPhrase"],
                    }
                )
        finally:
            write_inventory_pair(self.root, fixture_inventory())

        self.assertEqual(context.exception.code, "plan_changed")
        self.assertEqual(executions, [])

    def test_execution_rejects_original_plan_that_expires_during_refresh(self):
        executions = []
        state_holder = {}

        def expiring_refresh():
            state = state_holder["state"]
            for cached in state.plan_cache.values():
                cached["expiresAt"] = "2000-01-01T00:00:00+00:00"

        state = ControlState(
            project_root=self.root,
            refresh_runner=expiring_refresh,
            execution_runner=lambda plan: executions.append(plan["planId"]),
        )
        state_holder["state"] = state
        public = state.build_public_plan(
            {
                "snapshotId": SNAPSHOT_ID,
                "resourceIds": ["res_fixture"],
                "mode": "pause",
            }
        )

        with self.assertRaises(ApiError) as context:
            state.execute(
                {
                    "planId": public["planId"],
                    "confirmPhrase": public["confirmPhrase"],
                }
            )

        self.assertEqual(context.exception.code, "plan_expired")
        self.assertEqual(executions, [])

    def test_successful_execution_returns_before_post_refresh(self):
        refresh_count = 0

        def refresh():
            nonlocal refresh_count
            refresh_count += 1

        state = ControlState(
            project_root=self.root,
            refresh_runner=refresh,
            execution_runner=lambda plan: {
                "ok": True,
                "planId": plan["planId"],
                "mode": plan["mode"],
                "qbStopped": 1,
                "qbRemoved": 0,
                "filesDeleted": 0,
                "backupCreated": False,
            },
        )
        public = state.build_public_plan(
            {
                "snapshotId": SNAPSHOT_ID,
                "resourceIds": ["res_fixture"],
                "mode": "pause",
            }
        )
        result = state.execute(
            {
                "planId": public["planId"],
                "confirmPhrase": public["confirmPhrase"],
            }
        )

        self.assertTrue(result["snapshotRefreshPending"])
        self.assertEqual(refresh_count, 1)
        self.assertFalse(state.inventory_current)
        with self.assertRaises(ApiError) as context:
            state.build_public_plan(
                {
                    "snapshotId": SNAPSHOT_ID,
                    "resourceIds": ["res_fixture"],
                    "mode": "pause",
                }
            )
        self.assertEqual(context.exception.code, "inventory_stale")

    def test_enabled_execution_rechecks_and_writes_private_audit(self):
        executions = []
        refreshes = []

        def fake_refresh():
            refreshes.append(True)

        def fake_execution(plan):
            executions.append(plan["planId"])
            return {
                "ok": True,
                "planId": plan["planId"],
                "mode": plan["mode"],
                "qbStopped": 1,
                "qbRemoved": 0,
                "filesDeleted": 0,
                "backupCreated": False,
            }

        state = ControlState(
            project_root=self.root,
            refresh_runner=fake_refresh,
            execution_runner=fake_execution,
        )
        public = state.build_public_plan(
            {
                "snapshotId": SNAPSHOT_ID,
                "resourceIds": ["res_fixture"],
                "mode": "pause",
                "acknowledgeSiteRisk": False,
            }
        )
        result = state.execute(
            {
                "planId": public["planId"],
                "confirmPhrase": public["confirmPhrase"],
            }
        )

        self.assertTrue(result["ok"])
        self.assertEqual(executions, [public["planId"]])
        self.assertEqual(len(refreshes), 1)
        self.assertTrue(result["snapshotRefreshPending"])
        audit = self.root / ".runtime/execution-audit.jsonl"
        self.assertTrue(audit.is_file())
        self.assertEqual(audit.stat().st_mode & 0o777, 0o600)
        serialized = audit.read_text(encoding="utf-8")
        self.assertNotIn(TASK_HASH, serialized)
        self.assertNotIn("/mnt/", serialized)


if __name__ == "__main__":
    unittest.main()
