#!/opt/homebrew/bin/python3.12

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from execution_engine import REMOTE_EXECUTOR


TASK_HASH = "b" * 40
PLAN_ID = "plan_" + "c" * 24


class FakeQbState:
    def __init__(self):
        self.tasks = {
            TASK_HASH: {
                "hash": TASK_HASH,
                "name": "Fixture.Release",
                "save_path": "/fixture",
                "category": "test",
                "tags": "",
                "state": "stalledUP",
                "progress": 1,
                "force_start": False,
            }
        }
        self.lock = threading.Lock()
        self.fail_delete = False
        self.drop_delete_response = False
        self.fail_info_after_delete = False
        self.delete_callback = None


def fake_qb_handler(state: FakeQbState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):
            if urlparse(self.path).path != "/api/v2/torrents/info":
                self.send_error(404)
                return
            with state.lock:
                if state.fail_info_after_delete and TASK_HASH not in state.tasks:
                    self.send_error(503)
                    return
                body = json.dumps(list(state.tasks.values())).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            fields = parse_qs(self.rfile.read(length).decode())
            hashes = fields.get("hashes", [""])[0].split("|")
            path = urlparse(self.path).path
            with state.lock:
                if path == "/api/v2/torrents/stop":
                    for value in hashes:
                        if value in state.tasks:
                            state.tasks[value]["state"] = "stoppedUP"
                elif path == "/api/v2/torrents/start":
                    for value in hashes:
                        if value in state.tasks:
                            state.tasks[value]["state"] = "stalledUP"
                elif path == "/api/v2/torrents/setForceStart":
                    force = fields.get("value", ["false"])[0] == "true"
                    for value in hashes:
                        if value in state.tasks:
                            state.tasks[value]["force_start"] = force
                            if force:
                                state.tasks[value]["state"] = "forcedUP"
                elif path == "/api/v2/torrents/delete":
                    if state.fail_delete:
                        if state.delete_callback:
                            state.delete_callback()
                        self.send_error(500)
                        return
                    for value in hashes:
                        state.tasks.pop(value, None)
                    if state.delete_callback:
                        state.delete_callback()
                    if state.drop_delete_response:
                        self.connection.close()
                        return
                else:
                    self.send_error(404)
                    return
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


class RemoteExecutorSimulationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.sdc = self.root / "sdc"
        self.sdd = self.root / "sdd"
        self.download = self.sdc / "downloads/completed/Fixture"
        self.library = self.sdc / ".media-main/Movies/Fixture"
        self.download.mkdir(parents=True)
        self.library.mkdir(parents=True)
        self.payload = self.download / "Fixture.mkv"
        self.library_payload = self.library / "Fixture.mkv"
        self.payload.write_bytes(b"fixture payload")
        os.link(self.payload, self.library_payload)

        self.qb_backup = (
            self.root
            / "home/nas-user/.local/share/qBittorrent/BT_backup"
        )
        self.qb_backup.mkdir(parents=True)
        (self.qb_backup / f"{TASK_HASH}.torrent").write_bytes(b"torrent")
        (self.qb_backup / f"{TASK_HASH}.fastresume").write_bytes(b"resume")

        self.moviepilot_db = self.root / "moviepilot/config/user.db"
        self.moviepilot_db.parent.mkdir(parents=True)
        connection = sqlite3.connect(self.moviepilot_db)
        connection.execute(
            "create table mediaserveritem ("
            "id integer primary key, server text, item_id text, "
            "item_type text, title text, original_title text, year text, "
            "tmdbid integer, imdbid text, tvdbid text, path text, "
            "seasoninfo text)"
        )
        connection.execute(
            "insert into mediaserveritem values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                188,
                "jellyfin",
                "fixture-item",
                "电影",
                "Example",
                "Example",
                "2026",
                123,
                "tt123",
                "tv123",
                str(self.library),
                "",
            ),
        )
        connection.commit()
        connection.close()

        self.qb_state = FakeQbState()
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            fake_qb_handler(self.qb_state),
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def source(self):
        config = {
            "qb_url": f"http://127.0.0.1:{self.server.server_address[1]}",
            "qb_backup": str(self.qb_backup),
            "moviepilot_db": str(self.moviepilot_db),
            "execution_backup": str(self.root / "sdc/library-tools/storage-cleanup/qb-backups"),
            "allowed_roots": [
                str(self.sdc / "downloads/completed"),
                str(self.sdd / "downloads/completed"),
                str(self.sdc / ".media-main/Movies"),
                str(self.sdd / "media/TV"),
                str(self.sdc / "media/Movies"),
                str(self.sdc / "media/TV"),
                str(self.sdc / ".media-quarantine"),
                str(self.sdd / ".media-quarantine"),
            ],
            "quarantine_roots": {
                str(self.sdc): str(self.sdc / ".storage-cleanup-quarantine"),
                str(self.sdd): str(self.sdd / ".storage-cleanup-quarantine"),
            },
        }
        source = REMOTE_EXECUTOR.replace(
            'globals().get("__PINAS_CONFIG__", {})',
            repr(config),
        )
        source = source.replace(
            "def reconcile_removal(hashes, timeout=30):",
            "def reconcile_removal(hashes, timeout=2):",
        )
        return source

    def expectations(self):
        result = {}
        for path in (self.payload, self.library_payload):
            stat_result = path.stat()
            result[str(path)] = {
                "dev": stat_result.st_dev,
                "inode": stat_result.st_ino,
                "size": stat_result.st_size,
                "nlink": stat_result.st_nlink,
            }
        return result

    def run_executor(
        self,
        mode,
        *,
        files=False,
        include_task=True,
        include_moviepilot_index=False,
    ):
        operations = {
            "qbStop": (
                [TASK_HASH]
                if include_task and mode == "pause"
                else []
            ),
            "qbRemoveKeepFiles": (
                [TASK_HASH]
                if include_task and mode in {"retire", "delete"}
                else []
            ),
            "unlinkFiles": (
                [str(self.payload), str(self.library_payload)]
                if files
                else []
            ),
            "moviepilotIndexes": (
                [
                    {
                        "id": 188,
                        "server": "jellyfin",
                        "itemId": "fixture-item",
                        "itemType": "电影",
                        "title": "Example",
                        "originalTitle": "Example",
                        "year": "2026",
                        "path": str(self.library),
                        "seasonInfo": "",
                    }
                ]
                if include_moviepilot_index
                else []
            ),
        }
        completed = subprocess.run(
            [sys.executable, "-c", self.source()],
            input=json.dumps(
                {
                    "planId": PLAN_ID,
                    "mode": mode,
                    "operations": operations,
                    "fileExpectations": self.expectations()
                    if files
                    else {},
                }
            ),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        return completed, json.loads(completed.stdout)

    def run_recovery(self, action, confirm_phrase=""):
        completed = subprocess.run(
            [sys.executable, "-c", self.source()],
            input=json.dumps(
                {
                    "command": "recover",
                    "planId": PLAN_ID,
                    "action": action,
                    "confirmPhrase": confirm_phrase,
                }
            ),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        return completed, json.loads(completed.stdout)

    def test_pause_stops_task_and_preserves_every_file(self):
        completed, result = self.run_executor("pause")

        self.assertEqual(completed.returncode, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(self.qb_state.tasks[TASK_HASH]["state"], "stoppedUP")
        self.assertTrue(self.payload.exists())
        self.assertTrue(self.library_payload.exists())

    def test_remote_executor_rejects_mixed_mode_operations(self):
        completed = subprocess.run(
            [sys.executable, "-c", self.source()],
            input=json.dumps(
                {
                    "planId": PLAN_ID,
                    "mode": "pause",
                    "operations": {
                        "qbStop": [TASK_HASH],
                        "qbRemoveKeepFiles": [TASK_HASH],
                        "unlinkFiles": [],
                    },
                    "fileExpectations": {},
                }
            ),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        result = json.loads(completed.stdout)

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(result["error"]["code"], "invalid_operations")
        self.assertEqual(self.qb_state.tasks[TASK_HASH]["state"], "stalledUP")

    def test_retire_removes_task_but_keeps_files_and_backup(self):
        completed, result = self.run_executor("retire")

        self.assertEqual(completed.returncode, 0)
        self.assertTrue(result["ok"])
        self.assertNotIn(TASK_HASH, self.qb_state.tasks)
        self.assertTrue(self.payload.exists())
        self.assertTrue(self.library_payload.exists())
        backup = (
            self.sdc
            / "library-tools/storage-cleanup/qb-backups"
            / PLAN_ID
        )
        self.assertTrue((backup / f"{TASK_HASH}.torrent").is_file())
        self.assertTrue((backup / f"{TASK_HASH}.fastresume").is_file())
        self.assertEqual((backup / "manifest.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            json.loads((backup / "transaction.json").read_text())["phase"],
            "complete",
        )
        self.assertEqual(
            (backup / "transaction.json").stat().st_mode & 0o777,
            0o600,
        )

    def test_delete_removes_all_hardlinks_only_after_task_exit(self):
        completed, result = self.run_executor("delete", files=True)

        self.assertEqual(completed.returncode, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["filesDeleted"], 2)
        self.assertNotIn(TASK_HASH, self.qb_state.tasks)
        self.assertFalse(self.payload.exists())
        self.assertFalse(self.library_payload.exists())

    def test_file_only_delete_has_a_recoverable_transaction(self):
        completed, result = self.run_executor(
            "delete",
            files=True,
            include_task=False,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertTrue(result["ok"])
        self.assertTrue(result["backupCreated"])
        self.assertIn(TASK_HASH, self.qb_state.tasks)
        self.assertFalse(self.payload.exists())
        self.assertFalse(self.library_payload.exists())
        backup = (
            self.sdc
            / "library-tools/storage-cleanup/qb-backups"
            / PLAN_ID
        )
        manifest = json.loads((backup / "manifest.json").read_text())
        transaction = json.loads(
            (backup / "transaction.json").read_text()
        )
        self.assertEqual(manifest["tasks"], [])
        self.assertEqual(transaction["phase"], "complete")
        self.assertFalse(
            (self.sdc / ".storage-cleanup-quarantine" / PLAN_ID).exists()
        )

    def test_delete_removes_moviepilot_media_index_with_payload(self):
        completed, result = self.run_executor(
            "delete",
            files=True,
            include_task=False,
            include_moviepilot_index=True,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["moviepilotIndexesDeleted"], 1)
        connection = sqlite3.connect(self.moviepilot_db)
        self.assertIsNone(
            connection.execute(
                "select 1 from mediaserveritem where id=188"
            ).fetchone()
        )
        connection.close()
        backup = (
            self.sdc
            / "library-tools/storage-cleanup/qb-backups"
            / PLAN_ID
        )
        index_backup = json.loads(
            (backup / "moviepilot-index.json").read_text()
        )
        self.assertEqual(len(index_backup["rows"]), 1)
        self.assertFalse(self.payload.exists())
        self.assertFalse(self.library_payload.exists())

    def test_moviepilot_index_change_rolls_files_back(self):
        connection = sqlite3.connect(self.moviepilot_db)
        connection.execute(
            "update mediaserveritem set path=? where id=188",
            (str(self.download),),
        )
        connection.commit()
        connection.close()
        completed, result = self.run_executor(
            "delete",
            files=True,
            include_task=False,
            include_moviepilot_index=True,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(result["error"]["code"], "moviepilot_index_changed")
        self.assertTrue(self.payload.exists())
        self.assertTrue(self.library_payload.exists())

    def test_open_file_blocks_delete_and_restores_initial_qb_state(self):
        with self.payload.open("rb"):
            completed, result = self.run_executor("delete", files=True)

        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "file_in_use")
        self.assertEqual(self.qb_state.tasks[TASK_HASH]["state"], "stalledUP")
        self.assertTrue(self.payload.exists())
        self.assertTrue(self.library_payload.exists())

    def test_qb_remove_failure_rolls_staged_files_back(self):
        self.qb_state.tasks[TASK_HASH]["force_start"] = True
        self.qb_state.tasks[TASK_HASH]["state"] = "forcedUP"
        self.qb_state.fail_delete = True
        completed, result = self.run_executor("delete", files=True)

        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(result["ok"])
        self.assertEqual(self.qb_state.tasks[TASK_HASH]["state"], "forcedUP")
        self.assertTrue(self.qb_state.tasks[TASK_HASH]["force_start"])
        self.assertTrue(self.payload.exists())
        self.assertTrue(self.library_payload.exists())
        self.assertFalse(
            (self.sdc / ".storage-cleanup-quarantine" / PLAN_ID).exists()
        )

    def test_rollback_never_overwrites_a_new_source_file(self):
        self.qb_state.fail_delete = True
        self.qb_state.delete_callback = lambda: self.payload.write_bytes(
            b"new source"
        )

        completed, result = self.run_executor("delete", files=True)

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(result["error"]["code"], "rollback_incomplete")
        self.assertEqual(self.payload.read_bytes(), b"new source")
        quarantine = self.sdc / ".storage-cleanup-quarantine" / PLAN_ID
        self.assertEqual(len(list(quarantine.iterdir())), 1)

    def test_final_unlink_revalidates_quarantined_inode_state(self):
        def tamper_quarantine():
            quarantine = self.sdc / ".storage-cleanup-quarantine" / PLAN_ID
            first = sorted(quarantine.iterdir())[0]
            first.write_bytes(b"tampered payload")

        self.qb_state.delete_callback = tamper_quarantine

        completed, result = self.run_executor("delete", files=True)

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            result["error"]["code"],
            "quarantine_cleanup_incomplete",
        )
        quarantine = self.sdc / ".storage-cleanup-quarantine" / PLAN_ID
        self.assertEqual(len(list(quarantine.iterdir())), 2)

    def test_lost_delete_response_is_reconciled_as_committed(self):
        self.qb_state.drop_delete_response = True

        completed, result = self.run_executor("delete", files=True)

        self.assertEqual(completed.returncode, 0)
        self.assertTrue(result["ok"])
        self.assertNotIn(TASK_HASH, self.qb_state.tasks)
        self.assertFalse(self.payload.exists())
        self.assertFalse(self.library_payload.exists())

    def test_unreadable_delete_outcome_preserves_quarantined_files(self):
        self.qb_state.fail_info_after_delete = True

        completed, result = self.run_executor("delete", files=True)

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(result["error"]["code"], "qb_state_uncertain")
        self.assertNotIn(TASK_HASH, self.qb_state.tasks)
        self.assertFalse(self.payload.exists())
        self.assertFalse(self.library_payload.exists())
        quarantine = self.sdc / ".storage-cleanup-quarantine" / PLAN_ID
        self.assertEqual(len(list(quarantine.iterdir())), 2)
        backup = (
            self.sdc
            / "library-tools/storage-cleanup/qb-backups"
            / PLAN_ID
        )
        transaction = json.loads(
            (backup / "transaction.json").read_text()
        )
        self.assertEqual(transaction["phase"], "uncertain")
        self.assertEqual(len(transaction["stagedFiles"]), 2)
        self.assertEqual(
            {item["source"] for item in transaction["stagedFiles"]},
            {str(self.payload), str(self.library_payload)},
        )
        self.assertEqual(
            {item["quarantine"] for item in transaction["stagedFiles"]},
            {str(item) for item in quarantine.iterdir()},
        )
        self.qb_state.fail_info_after_delete = False
        inspected, inspection = self.run_recovery("inspect")
        finalized, final_result = self.run_recovery(
            "finalize",
            f"完成事务 {PLAN_ID}",
        )

        self.assertEqual(inspected.returncode, 0)
        self.assertEqual(inspection["tasksAbsent"], 1)
        self.assertEqual(inspection["filesQuarantined"], 2)
        self.assertEqual(finalized.returncode, 0)
        self.assertEqual(final_result["phase"], "complete")
        self.assertFalse(quarantine.exists())

    def test_recovery_finalize_resumes_after_one_hardlink_was_released(self):
        self.qb_state.fail_info_after_delete = True
        completed, result = self.run_executor("delete", files=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(result["error"]["code"], "qb_state_uncertain")
        quarantine = self.sdc / ".storage-cleanup-quarantine" / PLAN_ID
        sorted(quarantine.iterdir())[0].unlink()
        self.qb_state.fail_info_after_delete = False

        inspected, inspection = self.run_recovery("inspect")
        finalized, final_result = self.run_recovery(
            "finalize",
            f"完成事务 {PLAN_ID}",
        )

        self.assertEqual(inspected.returncode, 0)
        self.assertEqual(inspection["filesAlreadyGone"], 1)
        self.assertEqual(inspection["filesQuarantined"], 1)
        self.assertEqual(finalized.returncode, 0)
        self.assertEqual(final_result["phase"], "complete")
        self.assertFalse(quarantine.exists())

    def test_staging_manifest_precedes_the_first_file_move(self):
        crash_source = self.source().replace(
            "staged.append((source, destination))",
            "staged.append((source, destination))\n"
            "        if index == 0:\n"
            "            os._exit(91)",
            1,
        )
        completed = subprocess.run(
            [sys.executable, "-c", crash_source],
            input=json.dumps(
                {
                    "planId": PLAN_ID,
                    "mode": "delete",
                    "operations": {
                        "qbStop": [],
                        "qbRemoveKeepFiles": [TASK_HASH],
                        "unlinkFiles": [
                            str(self.payload),
                            str(self.library_payload),
                        ],
                    },
                    "fileExpectations": self.expectations(),
                }
            ),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

        self.assertEqual(completed.returncode, 91)
        backup = (
            self.sdc
            / "library-tools/storage-cleanup/qb-backups"
            / PLAN_ID
        )
        transaction = json.loads(
            (backup / "transaction.json").read_text()
        )
        self.assertEqual(transaction["phase"], "staging")
        self.assertEqual(len(transaction["stagedFiles"]), 2)
        first, second = transaction["stagedFiles"]
        self.assertFalse(Path(first["source"]).exists())
        self.assertTrue(Path(first["quarantine"]).exists())
        self.assertTrue(Path(second["source"]).exists())
        self.assertFalse(Path(second["quarantine"]).exists())
        recovered, recovery = self.run_recovery(
            "rollback",
            f"回滚事务 {PLAN_ID}",
        )

        self.assertEqual(recovered.returncode, 0)
        self.assertEqual(recovery["phase"], "rolled_back")
        self.assertTrue(self.payload.exists())
        self.assertTrue(self.library_payload.exists())
        self.assertEqual(
            self.qb_state.tasks[TASK_HASH]["state"],
            "stalledUP",
        )

    def test_crash_before_or_after_stop_has_a_rollback_record(self):
        injections = (
            (
                "before_stop",
                "    if hashes:\n"
                "        try:\n"
                "            qb_post(",
                "    if hashes:\n"
                "        os._exit(92)\n"
                "        try:\n"
                "            qb_post(",
                92,
            ),
            (
                "after_stop",
                "            wait_for_stopped(hashes)\n"
                "        except Exception:",
                "            wait_for_stopped(hashes)\n"
                "            os._exit(93)\n"
                "        except Exception:",
                93,
            ),
        )
        for label, marker, replacement, returncode in injections:
            with self.subTest(label=label):
                crash_source = self.source().replace(
                    marker,
                    replacement,
                    1,
                )
                completed = subprocess.run(
                    [sys.executable, "-c", crash_source],
                    input=json.dumps(
                        {
                            "planId": PLAN_ID,
                            "mode": "delete",
                            "operations": {
                                "qbStop": [],
                                "qbRemoveKeepFiles": [TASK_HASH],
                                "unlinkFiles": [
                                    str(self.payload),
                                    str(self.library_payload),
                                ],
                            },
                            "fileExpectations": self.expectations(),
                        }
                    ),
                    text=True,
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
                backup = (
                    self.sdc
                    / "library-tools/storage-cleanup/qb-backups"
                    / PLAN_ID
                )
                transaction = json.loads(
                    (backup / "transaction.json").read_text()
                )

                self.assertEqual(completed.returncode, returncode)
                self.assertEqual(transaction["phase"], "preparing")
                recovered, recovery = self.run_recovery(
                    "rollback",
                    f"回滚事务 {PLAN_ID}",
                )
                self.assertEqual(recovered.returncode, 0)
                self.assertEqual(recovery["phase"], "rolled_back")
                self.assertEqual(
                    self.qb_state.tasks[TASK_HASH]["state"],
                    "stalledUP",
                )
                self.assertTrue(self.payload.exists())
                self.assertTrue(self.library_payload.exists())

    def test_file_only_staging_crash_can_roll_back(self):
        crash_source = self.source().replace(
            "staged.append((source, destination))",
            "staged.append((source, destination))\n"
            "        if index == 0:\n"
            "            os._exit(91)",
            1,
        )
        completed = subprocess.run(
            [sys.executable, "-c", crash_source],
            input=json.dumps(
                {
                    "planId": PLAN_ID,
                    "mode": "delete",
                    "operations": {
                        "qbStop": [],
                        "qbRemoveKeepFiles": [],
                        "unlinkFiles": [
                            str(self.payload),
                            str(self.library_payload),
                        ],
                    },
                    "fileExpectations": self.expectations(),
                }
            ),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

        self.assertEqual(completed.returncode, 91)
        recovered, recovery = self.run_recovery(
            "rollback",
            f"回滚事务 {PLAN_ID}",
        )
        self.assertEqual(recovered.returncode, 0)
        self.assertEqual(recovery["taskCount"], 0)
        self.assertEqual(recovery["phase"], "rolled_back")
        self.assertTrue(self.payload.exists())
        self.assertTrue(self.library_payload.exists())
        self.assertIn(TASK_HASH, self.qb_state.tasks)

    def test_partial_stage_rollback_failure_stays_nonterminal(self):
        replacement = repr(str(self.payload))
        failure_source = self.source().replace(
            "validate_file(source_text, expected)",
            "validate_file(source_text, expected)\n"
            "        if index == 1:\n"
            f"            Path({replacement}).write_bytes(b'new source')\n"
            "            fail('injected_stage_failure', 'injected')",
            1,
        )
        completed = subprocess.run(
            [sys.executable, "-c", failure_source],
            input=json.dumps(
                {
                    "planId": PLAN_ID,
                    "mode": "delete",
                    "operations": {
                        "qbStop": [],
                        "qbRemoveKeepFiles": [TASK_HASH],
                        "unlinkFiles": [
                            str(self.payload),
                            str(self.library_payload),
                        ],
                    },
                    "fileExpectations": self.expectations(),
                }
            ),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        result = json.loads(completed.stdout)
        transaction = json.loads(
            (
                self.sdc
                / "library-tools/storage-cleanup/qb-backups"
                / PLAN_ID
                / "transaction.json"
            ).read_text()
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(result["error"]["code"], "rollback_incomplete")
        self.assertEqual(transaction["phase"], "rollback_incomplete")
        self.assertTrue(
            (self.sdc / ".storage-cleanup-quarantine" / PLAN_ID).is_dir()
        )

    def test_retry_reuses_verified_backup_after_safe_preflight_block(self):
        with self.payload.open("rb"):
            first, first_result = self.run_executor("delete", files=True)
        second, second_result = self.run_executor("delete", files=True)

        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(first_result["error"]["code"], "file_in_use")
        self.assertEqual(second.returncode, 0)
        self.assertTrue(second_result["ok"])
        self.assertFalse(self.payload.exists())
        self.assertFalse(self.library_payload.exists())

    def test_existing_quarantine_plan_directory_blocks_without_overwrite(self):
        collision = self.sdc / ".storage-cleanup-quarantine" / PLAN_ID
        collision.mkdir(parents=True)
        marker = collision / "do-not-overwrite"
        marker.write_text("preserve", encoding="utf-8")

        completed, result = self.run_executor("delete", files=True)

        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error"]["code"],
            "unresolved_transaction",
        )
        self.assertTrue(marker.is_file())
        self.assertTrue(self.payload.exists())
        self.assertTrue(self.library_payload.exists())
        self.assertEqual(self.qb_state.tasks[TASK_HASH]["state"], "stalledUP")

    def test_existing_unverified_backup_directory_is_preserved(self):
        collision = (
            self.sdc
            / "library-tools/storage-cleanup/qb-backups"
            / PLAN_ID
        )
        collision.mkdir(parents=True)
        marker = collision / "do-not-overwrite"
        marker.write_text("preserve", encoding="utf-8")

        completed, result = self.run_executor("retire")

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(result["error"]["code"], "unresolved_transaction")
        self.assertTrue(marker.is_file())
        self.assertIn(TASK_HASH, self.qb_state.tasks)
        self.assertEqual(self.qb_state.tasks[TASK_HASH]["state"], "stalledUP")


if __name__ == "__main__":
    unittest.main()
