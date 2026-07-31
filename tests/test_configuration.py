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


if __name__ == "__main__":
    unittest.main()
