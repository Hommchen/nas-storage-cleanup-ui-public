#!/opt/homebrew/bin/python3.12

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from action_planner import PlanInputError
from control_server import ApiError, ControlState
from test_control_server import SNAPSHOT_ID, fixture_inventory, write_inventory_pair


class ControlServerPlanRebuildTests(unittest.TestCase):
    def test_plan_input_error_during_preflight_returns_a_safe_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "public/data").mkdir(parents=True)
            (root / ".runtime").mkdir(parents=True)
            write_inventory_pair(root, fixture_inventory())
            executions = []
            state = ControlState(
                project_root=root,
                refresh_runner=lambda: None,
                execution_runner=lambda plan: executions.append(plan),
            )
            public = state.build_public_plan(
                {
                    "snapshotId": SNAPSHOT_ID,
                    "resourceIds": ["res_fixture"],
                    "mode": "pause",
                    "acknowledgeSiteRisk": False,
                }
            )

            with patch(
                "control_server.build_plan",
                side_effect=PlanInputError("forced rebuild failure"),
            ):
                with self.assertRaises(ApiError) as context:
                    state.execute(
                        {
                            "planId": public["planId"],
                            "confirmPhrase": public["confirmPhrase"],
                        }
                    )

            self.assertEqual(context.exception.status, 409)
            self.assertEqual(context.exception.code, "plan_rebuild_failed")
            self.assertEqual(context.exception.details, {})
            self.assertEqual(executions, [])


if __name__ == "__main__":
    unittest.main()
