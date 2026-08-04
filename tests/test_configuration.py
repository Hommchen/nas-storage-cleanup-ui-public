from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.configuration import (
    ConfigurationError,
    default_config,
    load_config,
    normalize_config,
    probe_config,
    write_config,
)


class ConfigurationTests(unittest.TestCase):
    def test_root_and_parent_traversal_are_rejected(self):
        config = default_config()
        config["allowed_roots"] = ["/"]
        with self.assertRaises(ConfigurationError):
            normalize_config(config)

        config = default_config()
        config["execution_backup"] = "/tmp/../unsafe"
        with self.assertRaises(ConfigurationError):
            normalize_config(config)

    def test_config_round_trip_is_atomic_and_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "shared/config.json"
            config = default_config()
            config["ssh_host"] = "nas@example.lan"
            written = write_config(path, config)
            self.assertEqual(load_config(path), written)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["ssh_host"], "nas@example.lan")

    def test_probe_is_read_only_and_checks_same_device_quarantine(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            volume = root / "volume"
            allowed = volume / "downloads"
            quarantine = volume / ".quarantine"
            qb_backup = volume / "qb-backup"
            execution_backup = volume / "execution-backup"
            for directory in (allowed, quarantine, qb_backup, execution_backup):
                directory.mkdir(parents=True)
            jellyfin_db = root / "jellyfin.db"
            moviepilot_db = root / "moviepilot.db"
            jellyfin_db.write_text("", encoding="utf-8")
            moviepilot_db.write_text("", encoding="utf-8")
            config = default_config()
            config.update(
                {
                    "jellyfin_db": str(jellyfin_db),
                    "moviepilot_db": str(moviepilot_db),
                    "qb_backup": str(qb_backup),
                    "execution_backup": str(execution_backup),
                    "allowed_roots": [str(allowed)],
                    "quarantine_roots": {str(volume): str(quarantine)},
                }
            )
            before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            result = probe_config(config)
            after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            self.assertTrue(result["ok"])
            self.assertEqual(result["missing"], [])
            self.assertEqual(before, after)

    def test_probe_allows_idle_quarantine_root_to_be_created_lazily(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            volume = root / "volume"
            allowed = volume / "downloads"
            qb_backup = volume / "qb-backup"
            execution_backup = volume / "execution-backup"
            for directory in (allowed, qb_backup, execution_backup):
                directory.mkdir(parents=True)
            jellyfin_db = root / "jellyfin.db"
            moviepilot_db = root / "moviepilot.db"
            jellyfin_db.write_text("", encoding="utf-8")
            moviepilot_db.write_text("", encoding="utf-8")
            quarantine = volume / ".quarantine"
            config = default_config()
            config.update(
                {
                    "jellyfin_db": str(jellyfin_db),
                    "moviepilot_db": str(moviepilot_db),
                    "qb_backup": str(qb_backup),
                    "execution_backup": str(execution_backup),
                    "allowed_roots": [str(allowed)],
                    "quarantine_roots": {str(volume): str(quarantine)},
                }
            )

            result = probe_config(config)

            self.assertTrue(result["ok"])
            self.assertEqual(result["missing"], [])
            quarantine_entry = next(
                entry for entry in result["entries"]
                if entry["kind"] == "quarantine_root"
            )
            self.assertFalse(quarantine_entry["exists"])
            self.assertTrue(quarantine_entry["missingAllowed"])
            self.assertFalse(quarantine.exists())

    def _volume_config(self, root: Path) -> dict:
        volume = root / "volume"
        allowed = volume / "downloads"
        quarantine = volume / ".quarantine"
        qb_backup = volume / "qb-backup"
        execution_backup = volume / "execution-backup"
        for directory in (allowed, qb_backup, execution_backup):
            directory.mkdir(parents=True)
        jellyfin_db = root / "jellyfin.db"
        moviepilot_db = root / "moviepilot.db"
        jellyfin_db.write_text("", encoding="utf-8")
        moviepilot_db.write_text("", encoding="utf-8")
        config = default_config()
        config.update(
            {
                "jellyfin_db": str(jellyfin_db),
                "moviepilot_db": str(moviepilot_db),
                "qb_backup": str(qb_backup),
                "execution_backup": str(execution_backup),
                "allowed_roots": [str(allowed)],
                "quarantine_roots": {str(volume): str(quarantine)},
            }
        )
        return config

    def test_hardlink_discovery_roots_default_to_empty_and_normalize(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = self._volume_config(Path(temporary))
            self.assertEqual(config["hardlink_discovery_roots"], [])
            normalized = normalize_config(config)
            self.assertEqual(normalized["hardlink_discovery_roots"], [])

    def test_probe_tolerates_missing_hardlink_discovery_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._volume_config(root)
            missing_discovery = root / "volume/.media-quarantine"
            config["hardlink_discovery_roots"] = [str(missing_discovery)]

            result = probe_config(config)

            self.assertTrue(result["ok"])
            self.assertEqual(result["missing"], [])
            entry = next(
                item for item in result["entries"]
                if item["kind"] == "hardlink_discovery_root"
            )
            self.assertFalse(entry["exists"])
            self.assertTrue(entry["missingAllowed"])

    def test_probe_reports_non_directory_discovery_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._volume_config(root)
            file_discovery = root / "volume/not-a-directory"
            file_discovery.write_text("", encoding="utf-8")
            config["hardlink_discovery_roots"] = [str(file_discovery)]

            result = probe_config(config)

            self.assertFalse(result["ok"])
            self.assertTrue(
                any("硬链接发现根不是目录" in item for item in result["problems"])
            )

    def test_hardlink_discovery_root_outside_volume_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._volume_config(root)
            config["hardlink_discovery_roots"] = [
                str(root / "elsewhere/quarantine")
            ]
            with self.assertRaises(ConfigurationError):
                normalize_config(config)

    def test_hardlink_discovery_root_cannot_cover_transaction_quarantine(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self._volume_config(root)
            nested = root / "volume/nested"
            nested.mkdir()
            config["quarantine_roots"] = {
                str(root / "volume"): str(nested / ".quarantine")
            }
            config["hardlink_discovery_roots"] = [
                str(nested)  # would cover nested/.quarantine
            ]
            with self.assertRaises(ConfigurationError):
                normalize_config(config)


if __name__ == "__main__":
    unittest.main()
