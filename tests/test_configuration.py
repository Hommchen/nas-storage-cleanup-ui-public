from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.configuration import (
    ConfigurationError,
    default_config,
    discover_config,
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

    def test_discovery_is_read_only_and_returns_a_ready_common_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            volume = root / "volume"
            media_movies = volume / "media/Movies"
            media_tv = volume / "media/TV"
            completed = volume / "downloads/completed"
            for directory in (media_movies, media_tv, completed):
                directory.mkdir(parents=True)
            qb_backup = root / "home/user/.local/share/qBittorrent/BT_backup"
            qb_backup.mkdir(parents=True)
            app_backup = root / "app/shared/qb-backups"
            app_backup.mkdir(parents=True)
            moviepilot_db = root / "config/user.db"
            moviepilot_db.parent.mkdir(parents=True)
            connection = sqlite3.connect(moviepilot_db)
            connection.execute("create table mediaserveritem (path text)")
            connection.execute(
                "insert into mediaserveritem values (?)",
                (str(media_movies / "Example" / "Example.mkv"),),
            )
            connection.commit()
            connection.close()
            jellyfin_db = root / "config/data/jellyfin.db"
            jellyfin_db.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(jellyfin_db)
            connection.execute("create table BaseItems (Id text)")
            connection.commit()
            connection.close()

            def fake_mountpoint(path):
                path = Path(path)
                return volume if path == volume or volume in path.parents else root

            before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
            discovery_current = default_config()
            discovery_current.update(
                {
                    "jellyfin_db": str(root / "missing-jellyfin.db"),
                    "moviepilot_db": str(root / "missing-moviepilot.db"),
                    "qb_backup": str(root / "missing-qb-backup"),
                    "qb_url": "http://127.0.0.1:9",
                }
            )
            with patch("scripts.discovery._DISCOVERY_ROOTS", (root,)), patch(
                "scripts.discovery._DISCOVERY_STORAGE_BASES", (root,)
            ), patch("scripts.discovery._DIRECT_DISCOVERY_CANDIDATES", {}), patch(
                "scripts.discovery._probe_qb_url", return_value=True
            ), patch("scripts.discovery._mountpoint", side_effect=fake_mountpoint):
                result = discover_config(discovery_current, project_root=root / "app")
            after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))

            self.assertTrue(result["readOnly"])
            self.assertTrue(result["ready"], result)
            self.assertEqual(before, after)
            self.assertEqual(result["config"]["moviepilot_db"], str(moviepilot_db.resolve()))
            self.assertEqual(result["config"]["jellyfin_db"], str(jellyfin_db.resolve()))
            self.assertEqual(result["config"]["qb_backup"], str(qb_backup.resolve()))
            self.assertIn(str(media_movies.resolve()), result["config"]["allowed_roots"])
            self.assertIn(str(media_tv.resolve()), result["config"]["allowed_roots"])

    def test_discovery_fails_closed_on_ambiguous_databases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            volume_one = root / "mnt/disk1"
            volume_two = root / "mnt/disk2"
            for volume in (volume_one, volume_two):
                (volume / "media/Movies").mkdir(parents=True)
                (volume / ".storage-cleanup-quarantine").mkdir()
            for path in (
                volume_one / "appdata/moviepilot/config/user.db",
                volume_two / "appdata/moviepilot/config/user.db",
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(path)
                connection.execute("create table mediaserveritem (path text)")
                connection.commit()
                connection.close()
            (root / "app/shared/qb-backups").mkdir(parents=True)

            def fake_mountpoint(path):
                path = Path(path)
                for volume in (volume_one, volume_two):
                    if path == volume or volume in path.parents:
                        return volume
                return root

            with patch("scripts.discovery._DISCOVERY_ROOTS", (root,)), patch(
                "scripts.discovery._DISCOVERY_STORAGE_BASES", (root / "mnt",)
            ), patch("scripts.discovery._DIRECT_DISCOVERY_CANDIDATES", {}), patch(
                "scripts.discovery._probe_qb_url", return_value=False
            ), patch("scripts.discovery._mountpoint", side_effect=fake_mountpoint):
                discovery_current = default_config()
                discovery_current.update(
                    {
                        "jellyfin_db": str(root / "missing-jellyfin.db"),
                        "moviepilot_db": str(root / "missing-moviepilot.db"),
                        "qb_backup": str(root / "missing-qb-backup"),
                        "qb_url": "http://127.0.0.1:9",
                    }
                )
                result = discover_config(discovery_current, project_root=root / "app")

            self.assertFalse(result["ready"])
            self.assertTrue(result["checks"][1]["ambiguous"])
            self.assertIn("moviepilot_db", result["ambiguities"])

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
