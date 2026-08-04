#!/opt/homebrew/bin/python3.12

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from action_planner import PlanInputError, build_plan, path_is_allowed, public_plan


NOW = datetime(2026, 7, 29, 0, 0, tzinfo=timezone.utc)


def task(*, task_hash: str = "a" * 40, hr: bool = False, private: bool = False):
    return {
        "hash": task_hash,
        "name": "Example.Release.2026",
        "site": "学校站" if private else "公开 BT",
        "scope": "整部",
        "state": "stalledUP",
        "progress": 1,
        "private": private,
        "hr": hr,
        "hrUnknown": False,
        "selfPublish": False,
        "contentPath": "/mnt/sdc/downloads/completed/Example/Example.mkv",
        "savePath": "/mnt/sdc/downloads/completed",
        "category": "",
        "tags": "",
        "fileListVerified": True,
        "exactFiles": [],
    }


def resource(
    resource_id: str,
    *,
    protected: bool = False,
    all_links_known: bool = True,
    tasks: list[dict] | None = None,
    file_path: str = "/mnt/sdc/downloads/completed/Example/Example.mkv",
):
    return {
        "id": resource_id,
        "title": "示例",
        "englishTitle": "Example",
        "edition": "电影",
        "sizeGiB": 1,
        "library": True,
        "hr": False,
        "brush": False,
        "protected": protected,
        "metadataVerified": True,
        "moviepilotIndexSourceAvailable": True,
        "moviepilotIndexes": [],
        "identity": f"movie:{resource_id}",
        "allLinksKnown": all_links_known,
        "files": [
            {
                "path": file_path,
                "dev": 1,
                "inode": 2,
                "size": 1024,
                "nlink": 1,
                "allowed": True,
            }
        ],
        "cleanupFiles": [
            {
                "path": file_path,
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
        "cleanupLinksKnown": all_links_known,
        "libraryScanVerified": True,
        "qbFileListsVerified": True,
        "roots": [
            {
                "path": str(PurePathParent(file_path)),
                "allowed": True,
            }
        ],
        "qbTasks": tasks or [],
    }


def PurePathParent(value: str) -> Path:
    return Path(value).parent


def inventory(*resources: dict):
    return {
        "schemaVersion": 2,
        "snapshotId": "snap_test",
        "generatedAt": "2026-07-29T00:00:00+00:00",
        "stats": {
            "hrSourceAvailable": True,
            "hrMissingUncovered": 0,
            "hrMissingUnassigned": 0,
        },
        "resources": {item["id"]: item for item in resources},
    }


class ActionPlannerTests(unittest.TestCase):
    def test_pause_plan_is_deterministic_and_non_mutating(self):
        source = inventory(resource("res_a", tasks=[task()]))
        before = deepcopy(source)
        first = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="pause",
            now=NOW,
        )
        second = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="pause",
            now=NOW,
        )

        self.assertEqual(first["planId"], second["planId"])
        self.assertTrue(first["canExecute"])
        self.assertEqual(first["operationCounts"] if "operationCounts" in first else 1, 1)
        self.assertEqual(first["operations"]["qbStop"], ["a" * 40])
        self.assertEqual(source, before)

    def test_multi_selection_counts_resources_and_deduplicates_tasks(self):
        first = resource("res_a", tasks=[task(task_hash="a" * 40)])
        second = resource("res_b", tasks=[task(task_hash="b" * 40)])
        shared = resource("res_c", tasks=[task(task_hash="a" * 40)])
        source = inventory(first, second, shared)

        plan = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_a", "res_b", "res_c"],
            mode="pause",
            now=NOW,
        )
        public = public_plan(plan)

        self.assertTrue(plan["canExecute"])
        self.assertEqual(plan["confirmPhrase"], "停止做种 3 项")
        self.assertEqual(len(plan["resources"]), 3)
        self.assertEqual(
            plan["operations"]["qbStop"],
            ["a" * 40, "b" * 40],
        )
        self.assertEqual(public["operationCounts"]["qbStop"], 2)

    def test_plan_id_ignores_unrelated_global_snapshot_changes(self):
        first_inventory = inventory(resource("res_a", tasks=[task()]))
        second_inventory = deepcopy(first_inventory)
        second_inventory["snapshotId"] = "snap_new_global_state"
        second_inventory["resources"]["res_unrelated"] = resource(
            "res_unrelated"
        )
        first = build_plan(
            first_inventory,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="pause",
            now=NOW,
        )
        second = build_plan(
            second_inventory,
            snapshot_id="snap_new_global_state",
            resource_ids=["res_a"],
            mode="pause",
            now=NOW,
        )

        self.assertEqual(first["planId"], second["planId"])
        self.assertTrue(second["canExecute"])

    def test_private_tracker_requires_explicit_acknowledgement(self):
        source = inventory(resource("res_a", tasks=[task(private=True)]))
        blocked = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="pause",
            now=NOW,
        )
        allowed = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="pause",
            acknowledge_site_risk=True,
            now=NOW,
        )

        self.assertFalse(blocked["canExecute"])
        self.assertIn(
            "site_risk_not_acknowledged",
            {item["code"] for item in blocked["blocks"]},
        )
        self.assertTrue(allowed["canExecute"])

    def test_delete_allows_and_warns_about_legacy_quarantine_hardlink(self):
        library_path = (
            "/mnt/sdc/.media-main/Movies/Fixture (2026)/Fixture.mkv"
        )
        legacy_path = (
            "/mnt/sdc/.media-quarantine/reconcile/Fixture (2026)/Fixture.mkv"
        )
        item = resource("res_a", file_path=library_path)
        item["files"][0]["nlink"] = 2
        item["cleanupFiles"][0]["nlink"] = 2
        item["cleanupFiles"][0]["allowed"] = True
        item["cleanupFiles"][0]["legacyQuarantine"] = False
        item["cleanupFiles"].append(
            {
                **item["cleanupFiles"][0],
                "path": legacy_path,
                "source": "hardlink",
                "allowed": False,
                "legacyQuarantine": True,
            }
        )
        source = inventory(item)

        plan = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="delete",
            now=NOW,
        )

        self.assertTrue(plan["canExecute"])
        self.assertIn(
            "legacy_quarantine_impact",
            {warning["code"] for warning in plan["warnings"]},
        )
        self.assertEqual(
            set(plan["operations"]["unlinkFiles"]),
            {library_path, legacy_path},
        )

    def test_delete_rejects_legacy_path_without_explicit_quarantine_flag(self):
        outside = resource("res_a")
        outside["cleanupFiles"][0]["allowed"] = False
        outside["cleanupFiles"][0]["legacyQuarantine"] = False
        outside["cleanupFiles"][0]["path"] = (
            "/mnt/sdc/.media-quarantine/reconcile/Fixture (2026)/Fixture.mkv"
        )
        source = inventory(outside)
        plan = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="delete",
            now=NOW,
        )

        self.assertFalse(plan["canExecute"])
        self.assertIn(
            "path_outside_allowlist",
            {item["code"] for item in plan["blocks"]},
        )

    def test_delete_allows_registered_legacy_quarantine_even_when_allowed_roots_exclude_it(self):
        library_path = (
            "/mnt/sdc/.media-main/Movies/Fixture (2026)/Fixture.mkv"
        )
        legacy_path = (
            "/mnt/sdc/.media-quarantine/reconcile/Fixture (2026)/Fixture.mkv"
        )
        item = resource("res_a", file_path=library_path)
        item["files"][0]["nlink"] = 2
        item["cleanupFiles"][0]["nlink"] = 2
        item["cleanupFiles"][0]["allowed"] = True
        item["cleanupFiles"][0]["legacyQuarantine"] = False
        item["cleanupFiles"].append(
            {
                **item["cleanupFiles"][0],
                "path": legacy_path,
                "source": "hardlink",
                "allowed": False,
                "legacyQuarantine": True,
            }
        )
        source = inventory(item)
        # The control server passes the configured allowed roots, which do
        # not include the legacy media quarantine.
        allowed_roots = (
            "/mnt/sdc/downloads/completed",
            "/mnt/sdc/.media-main/Movies",
        )

        plan = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="delete",
            allowed_roots=allowed_roots,
            now=NOW,
        )

        self.assertTrue(plan["canExecute"])
        self.assertIn(
            "legacy_quarantine_impact",
            {warning["code"] for warning in plan["warnings"]},
        )
        self.assertEqual(
            set(plan["operations"]["unlinkFiles"]),
            {library_path, legacy_path},
        )

    def test_hr_and_unfinished_resources_are_blocked(self):
        hr_task = task(hr=True, private=True)
        unfinished_task = task(task_hash="b" * 40)
        unfinished_task["progress"] = 0.5
        source = inventory(
            resource("res_hr", protected=True, tasks=[hr_task]),
            resource("res_dl", protected=True, tasks=[unfinished_task]),
        )
        plan = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_hr", "res_dl"],
            mode="retire",
            acknowledge_site_risk=True,
            now=NOW,
        )

        codes = {item["code"] for item in plan["blocks"]}
        self.assertFalse(plan["canExecute"])
        self.assertIn("hr_protected", codes)
        self.assertIn("unfinished_task", codes)

    def test_unverified_bilingual_identity_blocks_every_cleanup_level(self):
        selected = resource("res_unknown", tasks=[task()])
        selected["metadataVerified"] = False
        selected["protected"] = True
        source = inventory(selected)

        for mode in ("pause", "retire", "delete"):
            with self.subTest(mode=mode):
                plan = build_plan(
                    source,
                    snapshot_id="snap_test",
                    resource_ids=["res_unknown"],
                    mode=mode,
                    now=NOW,
                )
                self.assertFalse(plan["canExecute"])
                self.assertIn(
                    "metadata_unverified",
                    {item["code"] for item in plan["blocks"]},
                )

    def test_delete_requires_complete_hardlink_accounting(self):
        source = inventory(
            resource("res_a", all_links_known=False),
        )
        plan = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="delete",
            now=NOW,
        )

        self.assertFalse(plan["canExecute"])
        self.assertIn(
            "unknown_hardlinks",
            {item["code"] for item in plan["blocks"]},
        )

    def test_delete_rejects_paths_outside_allowlist(self):
        outside = resource("res_a", file_path="/etc/passwd")
        outside["cleanupFiles"][0]["allowed"] = False
        source = inventory(outside)
        plan = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="delete",
            now=NOW,
        )

        self.assertFalse(plan["canExecute"])
        self.assertIn(
            "path_outside_allowlist",
            {item["code"] for item in plan["blocks"]},
        )

    def test_delete_requires_verified_qb_file_list(self):
        selected = resource("res_a", tasks=[task()])
        selected["qbFileListsVerified"] = False
        source = inventory(selected)
        plan = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="delete",
            now=NOW,
        )

        self.assertFalse(plan["canExecute"])
        self.assertIn(
            "unverified_qb_file_list",
            {item["code"] for item in plan["blocks"]},
        )

    def test_delete_fails_closed_when_hr_source_is_unknown(self):
        unavailable = inventory(resource("res_a"))
        unavailable["stats"]["hrSourceAvailable"] = False
        source_unknown = build_plan(
            unavailable,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="delete",
            now=NOW,
        )
        self.assertIn(
            "hr_source_unavailable",
            {item["code"] for item in source_unknown["blocks"]},
        )

    def test_per_site_hr_failure_does_not_block_unrelated_public_bt_resource(self):
        source = inventory(resource("res_a", tasks=[task()]))
        source["stats"]["hrSourceAvailable"] = False
        source["stats"]["hrSources"] = {
            "学校站": {
                "supported": True,
                "available": False,
                "state": "stale",
            }
        }

        plan = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="delete",
            now=NOW,
        )

        self.assertTrue(plan["canExecute"])
        self.assertNotIn(
            "hr_source_unavailable",
            {item["code"] for item in plan["blocks"]},
        )

    def test_per_site_hr_failure_blocks_selected_private_resource(self):
        private_task = task(private=True)
        source = inventory(resource("res_a", tasks=[private_task]))
        source["stats"]["hrSourceAvailable"] = False
        source["stats"]["hrSources"] = {
            "学校站": {
                "supported": True,
                "available": False,
                "state": "stale",
            }
        }

        plan = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="delete",
            now=NOW,
        )

        self.assertFalse(plan["canExecute"])
        self.assertIn(
            "hr_source_unavailable",
            {item["code"] for item in plan["blocks"]},
        )

    def test_unassigned_hr_gap_warns_but_does_not_block_unrelated_delete(self):
        source = inventory(resource("res_a"))
        source["stats"]["hrMissingUnassigned"] = 1

        plan = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="delete",
            now=NOW,
        )

        self.assertTrue(plan["canExecute"])
        self.assertNotIn(
            "uncovered_hr_recovery",
            {item["code"] for item in plan["blocks"]},
        )
        self.assertIn(
            "unassigned_hr_recovery",
            {item["code"] for item in plan["warnings"]},
        )

    def test_unassigned_hr_gap_does_not_unlock_protected_resource(self):
        source = inventory(resource("res_a", protected=True))
        source["stats"]["hrMissingUnassigned"] = 1

        plan = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="delete",
            now=NOW,
        )

        self.assertFalse(plan["canExecute"])
        self.assertIn(
            "protected_resource",
            {item["code"] for item in plan["blocks"]},
        )

    def test_linked_missing_hr_does_not_block_unrelated_delete(self):
        source = inventory(resource("res_a"))
        source["stats"]["hrMissingUncovered"] = 4
        source["stats"]["hrMissingUnassigned"] = 0

        plan = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="delete",
            now=NOW,
        )

        self.assertTrue(plan["canExecute"])

    def test_unresolved_transaction_blocks_every_action_mode(self):
        source = inventory(resource("res_a", tasks=[task()]))
        source["stats"]["unresolvedTransactions"] = 1

        for mode in ("pause", "retire", "delete"):
            plan = build_plan(
                source,
                snapshot_id="snap_test",
                resource_ids=["res_a"],
                mode=mode,
                now=NOW,
            )
            self.assertFalse(plan["canExecute"])
            self.assertIn(
                "unresolved_transaction",
                {item["code"] for item in plan["blocks"]},
            )

    def test_delete_uses_exact_cleanup_files_not_display_inode_list(self):
        selected = resource("res_a")
        selected["files"][0]["path"] = (
            "/mnt/sdc/.media-main/Movies/Example/Example.mkv"
        )
        source = inventory(selected)
        plan = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="delete",
            now=NOW,
        )

        self.assertTrue(plan["canExecute"])
        self.assertEqual(
            plan["operations"]["unlinkFiles"],
            ["/mnt/sdc/downloads/completed/Example/Example.mkv"],
        )

    def test_delete_carries_moviepilot_index_identity_into_private_operation(self):
        selected = resource("res_a")
        selected["moviepilotIndexes"] = [
            {
                "id": 188,
                "server": "jellyfin",
                "itemId": "jellyfin-item-1",
                "itemType": "电视剧",
                "title": "Way of Choices",
                "originalTitle": "择天记",
                "year": "2026",
                "path": "/mnt/sdc/.media-main/Movies/Example",
                "seasonInfo": "{\"1\": [1]}",
            }
        ]
        source = inventory(selected)
        plan = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="delete",
            now=NOW,
        )

        self.assertTrue(plan["canExecute"])
        self.assertEqual(
            [item["id"] for item in plan["operations"]["moviepilotIndexes"]],
            [188],
        )
        self.assertEqual(public_plan(plan)["operationCounts"]["moviepilotIndexes"], 1)

    def test_delete_blocks_when_moviepilot_index_source_is_unavailable(self):
        selected = resource("res_a")
        selected["moviepilotIndexSourceAvailable"] = False
        plan = build_plan(
            inventory(selected),
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="delete",
            now=NOW,
        )

        self.assertFalse(plan["canExecute"])
        self.assertIn(
            "moviepilot_index_source_unavailable",
            {item["code"] for item in plan["blocks"]},
        )

    def test_stale_snapshot_and_duplicate_ids_are_rejected(self):
        source = inventory(resource("res_a"))
        stale = build_plan(
            source,
            snapshot_id="snap_old",
            resource_ids=["res_a"],
            mode="delete",
            now=NOW,
        )
        self.assertFalse(stale["canExecute"])
        self.assertIn("stale_snapshot", {item["code"] for item in stale["blocks"]})
        with self.assertRaises(PlanInputError):
            build_plan(
                source,
                snapshot_id="snap_test",
                resource_ids=["res_a", "res_a"],
                mode="delete",
                now=NOW,
            )

    def test_pause_with_no_qb_task_is_a_blocked_noop(self):
        source = inventory(resource("res_a"))
        plan = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="pause",
            now=NOW,
        )

        self.assertFalse(plan["canExecute"])
        self.assertIn("nothing_to_do", {item["code"] for item in plan["blocks"]})
        self.assertIn("no_qb_task", {item["code"] for item in plan["warnings"]})

    def test_public_plan_does_not_expose_hashes_or_paths(self):
        source = inventory(resource("res_a", tasks=[task()]))
        plan = build_plan(
            source,
            snapshot_id="snap_test",
            resource_ids=["res_a"],
            mode="pause",
            now=NOW,
        )
        public = public_plan(plan)
        serialized = str(public)

        self.assertNotIn("a" * 40, serialized)
        self.assertNotIn("/mnt/", serialized)
        self.assertEqual(public["operationCounts"]["qbStop"], 1)

    def test_path_allowlist_resists_prefix_and_parent_traversal(self):
        self.assertTrue(
            path_is_allowed("/mnt/sdc/downloads/completed/Title/file.mkv")
        )
        self.assertFalse(
            path_is_allowed("/mnt/sdc/downloads/completed-evil/file.mkv")
        )
        self.assertFalse(
            path_is_allowed("/mnt/sdc/downloads/completed/../secret/file.mkv")
        )
        self.assertFalse(path_is_allowed("mnt/sdc/downloads/completed/file.mkv"))


if __name__ == "__main__":
    unittest.main()
