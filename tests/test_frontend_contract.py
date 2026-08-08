#!/opt/homebrew/bin/python3.12

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FrontendContractTests(unittest.TestCase):
    def test_standalone_incomplete_filter_matches_snapshot_contract(self):
        source = (PROJECT_ROOT / "app/page.tsx").read_text(encoding="utf-8")

        self.assertIn("episodeIncomplete?: boolean;", source)
        self.assertIn("return item.episodeIncomplete === true;", source)
        self.assertNotIn(
            "item.incomplete === true || !item.library",
            source,
        )

    def test_both_frontends_explain_blocked_plans_without_claiming_execution(self):
        standalone = (PROJECT_ROOT / "app/page.tsx").read_text(encoding="utf-8")
        plugin = (
            PROJECT_ROOT / "plugins.v2/storagecleanup/src/components/AppPage.vue"
        ).read_text(encoding="utf-8")

        for source in (standalone, plugin):
            self.assertIn("安全可释放暂不可核算", source)
            self.assertIn("未生成可执行操作", source)
            self.assertIn("关联影响仅供复核，不会执行", source)
            self.assertIn("missingFiles", source)


if __name__ == "__main__":
    unittest.main()
