from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.render_systemd_units import UNIT_NAMES, render_directory, render_systemd_unit


class RenderSystemdUnitsTests(unittest.TestCase):
    def test_templates_render_to_target_context(self):
        source_dir = Path(__file__).resolve().parents[1] / "deploy/systemd"
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "rendered"
            render_directory(
                source_dir,
                output_dir,
                base="/srv/storage-cleanup-ui",
                user="nas-user",
                address="192.0.2.1",
            )
            for name in UNIT_NAMES:
                rendered = (output_dir / name).read_text(encoding="utf-8")
                self.assertNotIn("@PINAS_", rendered)
                self.assertIn("nas-user", rendered)
                self.assertIn("/srv/storage-cleanup-ui", rendered)
            gateway = (output_dir / "pinas-storage-cleanup-gateway.service").read_text(
                encoding="utf-8"
            )
            self.assertIn("--host 192.0.2.1 --port 3000", gateway)
            self.assertIn("--public-origin http://192.0.2.1:3000", gateway)

    def test_unrendered_marker_fails_closed(self):
        with self.assertRaises(ValueError):
            render_systemd_unit(
                "WorkingDirectory=@PINAS_BASE@\nUser=@PINAS_UNKNOWN@\n",
                base="/srv/cleanup",
                user="nas-user",
                address="127.0.0.1",
            )


if __name__ == "__main__":
    unittest.main()
