#!/opt/homebrew/bin/python3.12

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from execution_engine import (
    ExecutionError,
    LocalExecutionRunner,
    LocalRecoveryRunner,
    REMOTE_EXECUTOR,
    SSHExecutionRunner,
    SSHRecoveryRunner,
    validate_confirmation,
)


NOW = datetime(2026, 7, 29, 0, 0, tzinfo=timezone.utc)


def plan(*, can_execute: bool = True, expires_delta: timedelta = timedelta(minutes=5)):
    return {
        "planId": "plan_" + "a" * 24,
        "mode": "pause",
        "canExecute": can_execute,
        "confirmPhrase": "停止做种 1 项",
        "expiresAt": (NOW + expires_delta).isoformat(timespec="seconds"),
        "operations": {
            "qbStop": ["b" * 40],
            "qbRemoveKeepFiles": [],
            "unlinkFiles": [],
        },
        "fileExpectations": {},
    }


class ExecutionEngineTests(unittest.TestCase):
    def test_remote_executor_source_compiles(self):
        compile(REMOTE_EXECUTOR, "<remote-executor>", "exec")

    def test_confirmation_requires_exact_phrase(self):
        with self.assertRaises(ExecutionError) as context:
            validate_confirmation(
                plan(),
                confirm_phrase="停止做种 1项",
                now=NOW,
            )
        self.assertEqual(context.exception.code, "confirmation_mismatch")

    def test_blocked_and_expired_plans_never_execute(self):
        with self.assertRaises(ExecutionError) as blocked:
            validate_confirmation(
                plan(can_execute=False),
                confirm_phrase="停止做种 1 项",
                now=NOW,
            )
        with self.assertRaises(ExecutionError) as expired:
            validate_confirmation(
                plan(expires_delta=timedelta(seconds=-1)),
                confirm_phrase="停止做种 1 项",
                now=NOW,
            )
        self.assertEqual(blocked.exception.code, "plan_blocked")
        self.assertEqual(expired.exception.code, "plan_expired")

    def test_ssh_runner_sends_only_private_plan_payload(self):
        calls = []

        def fake_runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "planId": "plan_" + "a" * 24,
                        "mode": "pause",
                        "qbStopped": 1,
                        "qbRemoved": 0,
                        "filesDeleted": 0,
                        "backupCreated": False,
                    }
                ),
                stderr="",
            )

        runner = SSHExecutionRunner(
            host="test@example",
            command_runner=fake_runner,
        )
        result = runner(plan())
        payload = json.loads(calls[0][1]["input"])

        self.assertTrue(result["ok"])
        self.assertEqual(calls[0][0][0], "ssh")
        self.assertEqual(calls[0][0][3], "test@example")
        self.assertEqual(
            set(payload),
            {
                "planId",
                "mode",
                "operations",
                "fileExpectations",
                "missingFileExpectations",
            },
        )

    def test_ssh_runner_surfaces_structured_remote_failure(self):
        def fake_runner(command, **kwargs):
            return subprocess.CompletedProcess(
                command,
                2,
                stdout=json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "code": "file_in_use",
                            "message": "文件被占用。",
                        },
                    }
                ),
                stderr="",
            )

        with self.assertRaises(ExecutionError) as context:
            SSHExecutionRunner(command_runner=fake_runner)(plan())
        self.assertEqual(context.exception.code, "file_in_use")

    def test_recovery_runner_sends_only_explicit_recovery_request(self):
        calls = []

        def fake_runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "planId": "plan_" + "c" * 24,
                        "phase": "uncertain",
                    }
                ),
                stderr="",
            )

        result = SSHRecoveryRunner(
            host="test@example",
            command_runner=fake_runner,
        )(
            plan_id="plan_" + "c" * 24,
            action="inspect",
        )
        payload = json.loads(calls[0][1]["input"])

        self.assertTrue(result["ok"])
        self.assertEqual(
            payload,
            {
                "command": "recover",
                "planId": "plan_" + "c" * 24,
                "action": "inspect",
                "confirmPhrase": "",
            },
        )

    def test_local_runner_uses_isolated_sudo_python(self):
        calls = []

        def fake_runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "planId": "plan_" + "a" * 24,
                        "mode": "pause",
                        "qbStopped": 1,
                        "qbRemoved": 0,
                        "filesDeleted": 0,
                        "backupCreated": False,
                    }
                ),
                stderr="",
            )

        result = LocalExecutionRunner(command_runner=fake_runner)(plan())
        command, kwargs = calls[0]
        payload = json.loads(kwargs["input"])

        self.assertTrue(result["ok"])
        self.assertEqual(command[:4], ["sudo", "-n", "/usr/bin/python3", "-c"])
        self.assertNotIn("ssh", command)
        self.assertEqual(
            set(payload),
            {
                "planId",
                "mode",
                "operations",
                "fileExpectations",
                "missingFileExpectations",
            },
        )

    def test_local_recovery_runner_sends_explicit_request(self):
        calls = []

        def fake_runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "planId": "plan_" + "c" * 24,
                        "phase": "uncertain",
                    }
                ),
                stderr="",
            )

        result = LocalRecoveryRunner(command_runner=fake_runner)(
            plan_id="plan_" + "c" * 24,
            action="inspect",
        )
        payload = json.loads(calls[0][1]["input"])

        self.assertTrue(result["ok"])
        self.assertEqual(
            payload,
            {
                "command": "recover",
                "planId": "plan_" + "c" * 24,
                "action": "inspect",
                "confirmPhrase": "",
            },
        )


if __name__ == "__main__":
    unittest.main()
