from __future__ import annotations

import unittest

from scripts.configuration import ConfigurationError, default_config, normalize_config


class HitAndRunConfigurationTests(unittest.TestCase):
    def test_defaults_off_and_accepts_one_relative_path_per_site(self):
        config = default_config()
        self.assertFalse(config["hit_and_run_enabled"])
        self.assertEqual(config["hit_and_run_sites"][0]["path"], "/myhr.php")

        config.update(
            {
                "hit_and_run_enabled": True,
                "hit_and_run_sites": [
                    {
                        "site": "btschool.club",
                        "path": "/myhr.php",
                        "parser": "nexusphp_myhr",
                    },
                    {
                        "site": "crabpt.example",
                        "path": "/hr.php?page=1",
                        "parser": "nexusphp_myhr",
                    },
                ],
            }
        )
        normalized = normalize_config(config)
        self.assertTrue(normalized["hit_and_run_enabled"])
        self.assertEqual(len(normalized["hit_and_run_sites"]), 2)

    def test_path_rejects_external_urls_and_parent_traversal(self):
        config = default_config()
        config["hit_and_run_sites"] = [
            {
                "site": "btschool.club",
                "path": "https://example.com/myhr.php",
            }
        ]
        with self.assertRaises(ConfigurationError):
            normalize_config(config)

        config["hit_and_run_sites"][0]["path"] = "/foo/../myhr.php"
        with self.assertRaises(ConfigurationError):
            normalize_config(config)


if __name__ == "__main__":
    unittest.main()
