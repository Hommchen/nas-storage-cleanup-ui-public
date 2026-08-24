#!/opt/homebrew/bin/python3.12

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from snapshot_integrity import validate_snapshot_pair


def pair():
    stats = {
        "resources": 1,
        "hrActiveTitles": 1,
        "hrMatchedQbTasks": 1,
        "hrMissingQbTasks": 0,
        "metadataUnverifiedResources": 0,
    }
    public = {
        "schemaVersion": 2,
        "snapshotId": "snap_" + "a" * 24,
        "generatedAt": "2026-07-29T00:00:00+08:00",
        "stats": stats,
        "resources": [
            {
                "id": "res_a",
                "title": "示例",
                "englishTitle": "Example",
                "metadataVerified": True,
                "protected": False,
                "seedTasks": [],
            }
        ],
    }
    private = {
        "schemaVersion": 2,
        "snapshotId": public["snapshotId"],
        "generatedAt": public["generatedAt"],
        "stats": deepcopy(stats),
        "resources": {
            "res_a": {
                "id": "res_a",
                "metadataVerified": True,
            }
        },
    }
    return public, private


class SnapshotIntegrityTests(unittest.TestCase):
    def test_valid_pair_passes(self):
        validate_snapshot_pair(*pair())

    def test_empty_but_well_formed_inventory_passes(self):
        public, private = pair()
        public["resources"] = []
        private["resources"] = {}
        for stats in (public["stats"], private["stats"]):
            stats["resources"] = 0
            stats["hrActiveTitles"] = 0
            stats["hrMatchedQbTasks"] = 0

        validate_snapshot_pair(public, private)

    def test_public_private_ids_and_statistics_must_match(self):
        public, private = pair()
        private["resources"]["res_other"] = {
            "id": "res_other",
            "metadataVerified": True,
        }
        with self.assertRaises(ValueError):
            validate_snapshot_pair(public, private)

        public, private = pair()
        public["stats"]["resources"] = 2
        private["stats"]["resources"] = 2
        with self.assertRaises(ValueError):
            validate_snapshot_pair(public, private)

    def test_unverified_name_must_remain_locked(self):
        public, private = pair()
        public["resources"][0]["metadataVerified"] = False
        public["resources"][0]["protected"] = False
        private["resources"]["res_a"]["metadataVerified"] = False
        public["stats"]["metadataUnverifiedResources"] = 1
        private["stats"]["metadataUnverifiedResources"] = 1

        with self.assertRaises(ValueError):
            validate_snapshot_pair(public, private)

    def test_library_chinese_name_without_duplicate_english_name_is_valid(self):
        public, private = pair()
        public["resources"][0].update(
            {
                "title": "X战警：逆转未来",
                "englishTitle": "X战警：逆转未来",
                "library": True,
            }
        )
        validate_snapshot_pair(public, private)

        public, private = pair()
        public["resources"][0].update(
            {
                "title": "中文片名 1080p",
                "englishTitle": "中文片名 1080p",
                "library": False,
            }
        )
        with self.assertRaises(ValueError):
            validate_snapshot_pair(public, private)

    def test_provider_verified_english_only_name_is_valid(self):
        public, private = pair()
        public["resources"][0].update(
            {
                "title": "The Moon",
                "englishTitle": "The Moon",
                "library": True,
                "metadataProviderVerified": True,
            }
        )
        private["resources"]["res_a"]["metadataProviderVerified"] = True

        validate_snapshot_pair(public, private)

    def test_public_snapshot_rejects_paths_hashes_and_private_keys(self):
        for leak in (
            {"path": "/mnt/sdc/private.mkv"},
            {"hash": "a" * 40},
            {"_private": {"secret": True}},
        ):
            with self.subTest(leak=leak):
                public, private = pair()
                public["leak"] = leak
                with self.assertRaises(ValueError):
                    validate_snapshot_pair(public, private)

        public, private = pair()
        public["resources"][0]["mediaPath"] = ""
        with self.assertRaises(ValueError):
            validate_snapshot_pair(public, private)

    def test_hr_totals_must_close(self):
        public, private = pair()
        public["stats"]["hrMissingQbTasks"] = 1
        private["stats"]["hrMissingQbTasks"] = 1

        with self.assertRaises(ValueError):
            validate_snapshot_pair(public, private)


if __name__ == "__main__":
    unittest.main()
