#!/opt/homebrew/bin/python3.12
"""Confirmation-gated PiNAS cleanup execution with remote fail-closed checks."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hmac
import json
from pathlib import Path
import shlex
import subprocess
from typing import Any, Callable

try:
    from configuration import default_config
except ModuleNotFoundError:
    from scripts.configuration import default_config


REMOTE_EXECUTOR = r'''
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_CONFIG_DEFAULT = {
    "qb_url": "http://127.0.0.1:8080",
    "qb_backup": "/path/to/qBittorrent/BT_backup",
    "moviepilot_db": "/path/to/moviepilot/config/user.db",
    "execution_backup": "/path/to/storage-cleanup/qb-backups",
    "allowed_roots": [
        "/path/to/downloads/completed",
        "/path/to/media/movies",
        "/path/to/media/tv",
    ],
    "hardlink_discovery_roots": [],
    "quarantine_roots": {
        "/path/to": "/path/to/.storage-cleanup-quarantine",
    },
}
CONFIG = globals().get("__PINAS_CONFIG__", {}) or _CONFIG_DEFAULT
QB_URL = CONFIG["qb_url"]
QB_BACKUP = Path(CONFIG["qb_backup"])
MOVIEPILOT_DB = Path(CONFIG["moviepilot_db"])
EXECUTION_BACKUP = Path(CONFIG["execution_backup"])
ALLOWED_ROOTS = tuple(Path(value) for value in CONFIG["allowed_roots"])
LEGACY_QUARANTINE_ROOTS = tuple(
    Path(value)
    for value in (CONFIG.get("hardlink_discovery_roots") or ())
    if "/.media-quarantine/" in str(value) + "/"
)
QUARANTINE_ROOTS = {
    str(volume): Path(value)
    for volume, value in CONFIG["quarantine_roots"].items()
}
HASH_RE = re.compile(r"^[0-9a-fA-F]{40}$")
PLAN_RE = re.compile(r"^plan_[0-9a-f]{24}$")


class RemoteFailure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def fail(code, message):
    raise RemoteFailure(code, message)


def write_private_json(path, payload):
    if path.is_symlink() or path.parent.is_symlink() or not path.parent.is_dir():
        fail("unsafe_backup_path", "事务记录路径不可信。")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def unresolved_transactions():
    unresolved = set()
    if EXECUTION_BACKUP.is_dir() and not EXECUTION_BACKUP.is_symlink():
        for child in EXECUTION_BACKUP.iterdir():
            if child.is_symlink() or not child.is_dir():
                unresolved.add(child.name)
                continue
            state_path = child / "transaction.json"
            if state_path.is_symlink() or not state_path.is_file():
                unresolved.add(child.name)
                continue
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                unresolved.add(child.name)
                continue
            if state.get("phase") not in {"complete", "rolled_back"}:
                unresolved.add(child.name)
    for root in QUARANTINE_ROOTS.values():
        if not root.is_dir() or root.is_symlink():
            continue
        for child in root.iterdir():
            unresolved.add(child.name)
    return unresolved


def qb_json(path):
    with urlopen(QB_URL + path, timeout=45) as response:
        return json.load(response)


def qb_post(path, fields):
    body = urlencode(fields).encode("utf-8")
    request = Request(
        QB_URL + path,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(request, timeout=45) as response:
        response.read()


def all_torrents():
    return {
        str(item.get("hash") or "").lower(): item
        for item in qb_json("/api/v2/torrents/info")
    }


def is_stopped(state):
    value = str(state or "")
    return value.startswith("stopped") or value.startswith("paused")


def wait_for_stopped(hashes, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = all_torrents()
        if all(
            task_hash in current
            and is_stopped(current[task_hash].get("state"))
            for task_hash in hashes
        ):
            return
        time.sleep(0.5)
    fail("qb_stop_timeout", "qB 任务未能在安全时限内停止。")


def wait_for_absent(hashes, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = all_torrents()
        if all(task_hash not in current for task_hash in hashes):
            return
        time.sleep(0.5)
    fail("qb_remove_timeout", "qB 任务退出后仍可见，已停止后续操作。")


def reconcile_removal(hashes, timeout=30):
    """Classify an ambiguous qB delete as absent, present, or unknown."""

    deadline = time.monotonic() + timeout
    consecutive_present = 0
    while time.monotonic() < deadline:
        try:
            current = all_torrents()
        except Exception:
            consecutive_present = 0
            time.sleep(0.5)
            continue
        if all(task_hash not in current for task_hash in hashes):
            return "absent"
        if all(task_hash in current for task_hash in hashes):
            consecutive_present += 1
            if consecutive_present >= 3:
                return "present"
        else:
            consecutive_present = 0
        time.sleep(0.5)
    return "unknown"


def start_hashes(hashes):
    if hashes:
        qb_post("/api/v2/torrents/start", {"hashes": "|".join(hashes)})


def restore_run_states(tasks, hashes):
    if not hashes:
        return
    start_hashes(hashes)
    forced = [
        task_hash
        for task_hash in hashes
        if bool(tasks.get(task_hash, {}).get("force_start"))
    ]
    if forced:
        qb_post(
            "/api/v2/torrents/setForceStart",
            {"hashes": "|".join(forced), "value": "true"},
        )


def validate_hashes(hashes):
    normalized = []
    for value in hashes:
        task_hash = str(value or "").lower()
        if not HASH_RE.fullmatch(task_hash):
            fail("invalid_qb_hash", "执行计划包含无效的 qB 标识。")
        normalized.append(task_hash)
    if len(normalized) != len(set(normalized)):
        fail("duplicate_qb_hash", "执行计划包含重复的 qB 任务。")
    return normalized


def path_allowed(value):
    path = Path(str(value or ""))
    if not path.is_absolute() or ".." in path.parts:
        return False
    try:
        resolved = Path(os.path.realpath(path))
    except OSError:
        return False
    return any(
        resolved == root or root in resolved.parents
        for root in ALLOWED_ROOTS
    )


def legacy_quarantine_allowed(value):
    """Allow a plan-registered hard link that lives under a legacy media
    quarantine discovery root (for example /.media-quarantine/)."""
    path = Path(str(value or ""))
    if not path.is_absolute() or ".." in path.parts:
        return False
    try:
        resolved = Path(os.path.realpath(path))
    except OSError:
        return False
    return any(
        resolved == root or root in resolved.parents
        for root in LEGACY_QUARANTINE_ROOTS
    )


MOVIEPILOT_INDEX_COLUMNS = (
    "id",
    "server",
    "item_id",
    "item_type",
    "title",
    "original_title",
    "year",
    "tmdbid",
    "imdbid",
    "tvdbid",
    "path",
    "seasoninfo",
)


def moviepilot_db():
    if (
        MOVIEPILOT_DB.is_symlink()
        or not MOVIEPILOT_DB.is_file()
        or MOVIEPILOT_DB.parent.is_symlink()
    ):
        fail("moviepilot_index_unavailable", "MoviePilot 媒体库索引不可验证。")
    try:
        connection = sqlite3.connect(str(MOVIEPILOT_DB), timeout=30)
        connection.execute("pragma busy_timeout=30000")
        connection.row_factory = sqlite3.Row
        columns = {
            str(row[1])
            for row in connection.execute(
                "pragma table_info('mediaserveritem')"
            )
        }
    except sqlite3.Error:
        fail("moviepilot_index_unavailable", "MoviePilot 媒体库索引不可验证。")
    if not set(MOVIEPILOT_INDEX_COLUMNS).issubset(columns):
        connection.close()
        fail("moviepilot_index_schema", "MoviePilot 媒体库索引结构不受支持。")
    return connection


def moviepilot_expected_index(raw):
    if not isinstance(raw, dict):
        fail("moviepilot_index_invalid", "执行计划包含无效的 MoviePilot 索引。")
    try:
        index_id = int(raw.get("id"))
    except (TypeError, ValueError):
        index_id = 0
    path = str(raw.get("path") or "")
    if index_id <= 0 or not path_allowed(path):
        fail("moviepilot_index_invalid", "MoviePilot 索引身份或路径不可信。")
    return {
        "id": index_id,
        "server": str(raw.get("server") or "jellyfin"),
        "item_id": str(raw.get("itemId", raw.get("item_id")) or ""),
        "item_type": str(raw.get("itemType", raw.get("item_type")) or ""),
        "title": str(raw.get("title") or ""),
        "original_title": str(
            raw.get("originalTitle", raw.get("original_title")) or ""
        ),
        "year": str(raw.get("year") or ""),
        "path": path,
        "seasoninfo": str(
            raw.get("seasonInfo", raw.get("seasoninfo")) or ""
        ),
    }


def moviepilot_row_matches(row, expected):
    if row is None:
        return False
    for key in (
        "id",
        "server",
        "item_id",
        "item_type",
        "title",
        "original_title",
        "year",
        "path",
        "seasoninfo",
    ):
        actual = "" if row[key] is None else str(row[key])
        if key == "id":
            if int(row[key]) != int(expected[key]):
                return False
        elif actual != str(expected[key]):
            return False
    return True


def moviepilot_row_dict(row):
    return {
        key: row[key]
        for key in MOVIEPILOT_INDEX_COLUMNS
    }


def moviepilot_indexes_from_transaction(transaction):
    raw = transaction.get("moviepilotIndexRows") or []
    if not isinstance(raw, list):
        fail("recovery_state_invalid", "MoviePilot 索引事务记录格式不正确。")
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            fail("recovery_state_invalid", "MoviePilot 索引事务记录格式不正确。")
        if any(key not in item for key in MOVIEPILOT_INDEX_COLUMNS):
            fail("recovery_state_invalid", "MoviePilot 索引事务记录不完整。")
        rows.append({key: item[key] for key in MOVIEPILOT_INDEX_COLUMNS})
    return rows


def restore_moviepilot_indexes(rows):
    if not rows:
        return
    connection = moviepilot_db()
    placeholders = ",".join("?" for _ in MOVIEPILOT_INDEX_COLUMNS)
    try:
        connection.execute("begin immediate")
        for item in rows:
            current = connection.execute(
                "select " + ",".join(MOVIEPILOT_INDEX_COLUMNS)
                + " from mediaserveritem where id=?",
                (int(item["id"]),),
            ).fetchone()
            if current is not None:
                if not moviepilot_row_matches(current, item):
                    fail("moviepilot_index_conflict", "MoviePilot 索引恢复目标已被占用。")
                continue
            connection.execute(
                "insert into mediaserveritem ("
                + ",".join(MOVIEPILOT_INDEX_COLUMNS)
                + ") values ("
                + placeholders
                + ")",
                tuple(item[key] for key in MOVIEPILOT_INDEX_COLUMNS),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def remove_moviepilot_indexes(raw_indexes, backup_dir):
    if not raw_indexes:
        return []
    expected_indexes = [moviepilot_expected_index(item) for item in raw_indexes]
    if len({item["id"] for item in expected_indexes}) != len(expected_indexes):
        fail("duplicate_moviepilot_index", "执行计划包含重复的 MoviePilot 索引。")
    connection = moviepilot_db()
    select_sql = (
        "select " + ",".join(MOVIEPILOT_INDEX_COLUMNS)
        + " from mediaserveritem where id=?"
    )
    rows = []
    try:
        connection.execute("begin immediate")
        for expected in expected_indexes:
            current = connection.execute(select_sql, (expected["id"],)).fetchone()
            if current is None:
                continue
            if not moviepilot_row_matches(current, expected):
                fail("moviepilot_index_changed", "MoviePilot 索引已变化，请重新生成计划。")
            rows.append(moviepilot_row_dict(current))
        write_private_json(
            backup_dir / "moviepilot-index.json",
            {"planId": backup_dir.name, "rows": rows},
        )
        for item in rows:
            connection.execute(
                "delete from mediaserveritem where id=?",
                (int(item["id"]),),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    connection = moviepilot_db()
    try:
        remaining = [
            item["id"]
            for item in rows
            if connection.execute(
                "select 1 from mediaserveritem where id=?",
                (int(item["id"]),),
            ).fetchone()
        ]
    finally:
        connection.close()
    if remaining:
        fail("moviepilot_index_remove_unverified", "MoviePilot 媒体库索引未完全清理。")
    return rows


def validate_file(path, expected):
    if not path_allowed(path) and not legacy_quarantine_allowed(path):
        fail("path_outside_allowlist", "文件已离开允许清理的目录。")
    candidate = Path(path)
    try:
        lstat = candidate.lstat()
        current = candidate.stat()
    except FileNotFoundError:
        fail("file_missing", "文件状态已变化，请重新生成计划。")
    except OSError:
        fail("file_unreadable", "无法复核文件状态，已拒绝删除。")
    if stat.S_ISLNK(lstat.st_mode) or not stat.S_ISREG(current.st_mode):
        fail("non_regular_file", "清理目标不是普通文件，已拒绝删除。")
    actual = {
        "dev": int(current.st_dev),
        "inode": int(current.st_ino),
        "size": int(current.st_size),
        "nlink": int(current.st_nlink),
    }
    if any(int(expected.get(key) or -1) != actual[key] for key in actual):
        fail("file_state_changed", "文件或硬链接状态已变化，请重新生成计划。")
    return actual


def validate_files(paths, expectations):
    inode_paths = {}
    inode_links = {}
    for path in paths:
        expected = expectations.get(path)
        if not isinstance(expected, dict):
            fail("missing_file_expectation", "执行计划缺少文件校验信息。")
        actual = validate_file(path, expected)
        inode = (actual["dev"], actual["inode"])
        inode_paths.setdefault(inode, set()).add(path)
        inode_links[inode] = actual["nlink"]
    if any(
        len(inode_paths[inode]) != inode_links[inode]
        for inode in inode_paths
    ):
        fail("hardlink_set_changed", "硬链接集合不完整，已拒绝删除。")


def open_handle_count(paths):
    targets = {os.path.realpath(path) for path in paths}
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        lsof = shutil.which("lsof")
        if not lsof:
            fail("open_handle_check_failed", "无法确认文件是否被占用。")
        try:
            completed = subprocess.run(
                [lsof, "-Fn", "--", *sorted(targets)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            fail("open_handle_check_failed", "无法确认文件是否被占用。")
        return sum(
            1
            for line in completed.stdout.splitlines()
            if line.startswith("n")
            and os.path.realpath(line[1:]) in targets
        )
    count = 0
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        fd_dir = proc / "fd"
        try:
            descriptors = list(fd_dir.iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if target.endswith(" (deleted)"):
                target = target[:-10]
            if os.path.realpath(target) in targets:
                count += 1
    return count


def backup_qb_state(plan_id, hashes, tasks, *, copy_files):
    if EXECUTION_BACKUP.is_symlink():
        fail("unsafe_backup_path", "qB 备份根目录不是可信普通目录。")
    EXECUTION_BACKUP.mkdir(parents=True, exist_ok=True)
    if EXECUTION_BACKUP.is_symlink() or not EXECUTION_BACKUP.is_dir():
        fail("unsafe_backup_path", "qB 备份根目录不是可信普通目录。")
    os.chmod(EXECUTION_BACKUP, 0o700)
    backup_dir = EXECUTION_BACKUP / plan_id
    if backup_dir.is_symlink():
        fail("unsafe_backup_path", "qB 备份目录不是可信普通目录。")
    created_directory = False
    if backup_dir.is_dir():
        manifest_path = backup_dir / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            fail("backup_collision", "已有 qB 备份无法验证，已拒绝覆盖。")
        manifest_hashes = {
            str(item.get("hash") or "")
            for item in manifest.get("tasks") or []
            if isinstance(item, dict)
        }
        if (
            manifest.get("planId") != plan_id
            or manifest_hashes != set(hashes)
            or manifest_path.is_symlink()
        ):
            fail("backup_collision", "已有 qB 备份与当前计划不一致。")
    elif backup_dir.exists():
        fail("backup_collision", "qB 备份目标已存在且不是普通目录。")
    else:
        backup_dir.mkdir(parents=True, exist_ok=False)
        os.chmod(backup_dir, 0o700)
        created_directory = True
    manifest = {
        "planId": plan_id,
        "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tasks": [
            {
                "hash": task_hash,
                "name": str(tasks[task_hash].get("name") or ""),
                "savePath": str(tasks[task_hash].get("save_path") or ""),
                "category": str(tasks[task_hash].get("category") or ""),
                "tags": str(tasks[task_hash].get("tags") or ""),
                "state": str(tasks[task_hash].get("state") or ""),
                "forceStart": bool(
                    tasks[task_hash].get("force_start")
                ),
            }
            for task_hash in hashes
        ],
    }
    manifest_path = backup_dir / "manifest.json"
    write_private_json(manifest_path, manifest)
    if not copy_files:
        return backup_dir
    try:
        for task_hash in hashes:
            torrent = QB_BACKUP / f"{task_hash}.torrent"
            resume = QB_BACKUP / f"{task_hash}.fastresume"
            if (
                not torrent.is_file()
                or torrent.is_symlink()
                or not resume.is_file()
                or resume.is_symlink()
            ):
                fail(
                    "qb_backup_missing",
                    "qB 原始种子或恢复状态缺失，已拒绝退出任务。",
                )
            for source in (torrent, resume):
                destination = backup_dir / source.name
                if destination.is_symlink() or (
                    destination.exists() and not destination.is_file()
                ):
                    fail("unsafe_backup_path", "qB 备份目标不可信。")
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(
                        "wb",
                        dir=backup_dir,
                        prefix=f".{source.name}.",
                        suffix=".tmp",
                        delete=False,
                    ) as handle:
                        temporary = Path(handle.name)
                        with source.open("rb") as source_handle:
                            shutil.copyfileobj(source_handle, handle)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, destination)
                    os.chmod(destination, 0o600)
                except Exception:
                    if temporary is not None:
                        try:
                            temporary.unlink(missing_ok=True)
                        except OSError:
                            pass
                    raise
    except Exception:
        if created_directory:
            for created in backup_dir.iterdir():
                if created.is_file() and not created.is_symlink():
                    created.unlink()
            backup_dir.rmdir()
        raise
    return backup_dir


def write_transaction_state(
    backup_dir,
    phase,
    *,
    detail=None,
    staged=None,
    fields=None,
    reset=False,
):
    if backup_dir is None:
        return
    if backup_dir.is_symlink() or not backup_dir.is_dir():
        fail("unsafe_backup_path", "qB 事务记录目录不可信。")
    state_path = backup_dir / "transaction.json"
    if state_path.is_symlink():
        fail("unsafe_backup_path", "qB 事务记录文件不可信。")
    payload = {}
    if state_path.is_file() and not reset:
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            fail("unsafe_backup_path", "qB 事务记录无法验证。")
        if not isinstance(payload, dict):
            fail("unsafe_backup_path", "qB 事务记录格式不可信。")
    payload.update({
        "phase": phase,
        "updatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    if fields:
        payload.update(fields)
    if detail:
        payload["detail"] = detail
    if staged is not None:
        staged_files = []
        for source, destination in staged:
            try:
                current = destination.stat()
            except OSError:
                fail("staged_file_unreadable", "无法记录隔离文件状态。")
            staged_files.append(
                {
                    "source": str(source),
                    "quarantine": str(destination),
                    "dev": int(current.st_dev),
                    "inode": int(current.st_ino),
                    "size": int(current.st_size),
                    "nlink": int(current.st_nlink),
                }
            )
        payload["stagedFiles"] = staged_files
    write_private_json(state_path, payload)


def quarantine_root(path, plan_id):
    resolved = Path(os.path.realpath(path))
    for disk, root in QUARANTINE_ROOTS.items():
        disk_path = Path(disk)
        if resolved == disk_path or disk_path in resolved.parents:
            return root / plan_id
    fail("unknown_filesystem", "文件不在已知数据盘上。")


def staging_manifest(paths, plan_id, expectations):
    records = []
    for index, source_text in enumerate(paths):
        source = Path(source_text)
        expected = expectations.get(source_text)
        if not isinstance(expected, dict):
            fail("missing_file_expectation", "执行计划缺少文件校验信息。")
        root = quarantine_root(source, plan_id)
        token = hashlib.sha256(source_text.encode("utf-8")).hexdigest()[:16]
        records.append(
            {
                "source": source_text,
                "quarantine": str(root / f"{index:06d}-{token}"),
                **{
                    key: int(expected.get(key) or -1)
                    for key in ("dev", "inode", "size", "nlink")
                },
            }
        )
    return records


def stage_files(paths, plan_id, expectations, staged):
    created_roots = set()
    for index, source_text in enumerate(paths):
        source = Path(source_text)
        expected = expectations.get(source_text)
        if not isinstance(expected, dict):
            fail("missing_file_expectation", "执行计划缺少文件校验信息。")
        validate_file(source_text, expected)
        root = quarantine_root(source, plan_id)
        if root.parent.is_symlink() or root.is_symlink():
            fail("unsafe_quarantine_path", "隔离目录不是可信普通目录。")
        if root not in created_roots:
            root.parent.mkdir(parents=True, exist_ok=True)
            if root.parent.is_symlink():
                fail("unsafe_quarantine_path", "隔离目录不是可信普通目录。")
            os.chmod(root.parent, 0o700)
            root.mkdir(exist_ok=False)
            created_roots.add(root)
        if root.is_symlink() or not root.is_dir():
            fail("unsafe_quarantine_path", "隔离目录不是可信普通目录。")
        os.chmod(root, 0o700)
        token = hashlib.sha256(source_text.encode("utf-8")).hexdigest()[:16]
        destination = root / f"{index:06d}-{token}"
        if destination.exists() or destination.is_symlink():
            fail("quarantine_collision", "隔离目标已经存在，已拒绝覆盖。")
        os.rename(source, destination)
        staged.append((source, destination))
        try:
            lstat = destination.lstat()
            current = destination.stat()
        except OSError:
            fail("staged_file_unreadable", "隔离后的文件无法复核。")
        actual = {
            "dev": int(current.st_dev),
            "inode": int(current.st_ino),
            "size": int(current.st_size),
            "nlink": int(current.st_nlink),
        }
        if (
            stat.S_ISLNK(lstat.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or any(
                int(expected.get(key) or -1) != actual[key]
                for key in actual
            )
        ):
            fail("staged_file_changed", "隔离后的文件状态不一致。")
    return staged


def rollback_staged(staged, expectations):
    failures = 0
    for source, destination in reversed(staged):
        try:
            if source.exists() or source.is_symlink():
                failures += 1
                continue
            expected = expectations.get(str(source))
            if not isinstance(expected, dict):
                failures += 1
                continue
            if not validate_recovery_file(destination, expected):
                failures += 1
                continue
            ensure_restore_parent(source)
            os.rename(destination, source)
        except Exception:
            failures += 1
    return failures


def remove_empty_quarantine_roots(staged):
    for root in sorted({destination.parent for _, destination in staged}):
        try:
            root.rmdir()
            root.parent.rmdir()
        except OSError:
            pass


def quarantine_roots_clean(staged):
    return all(
        not destination.exists()
        and not destination.is_symlink()
        and not destination.parent.exists()
        for _, destination in staged
    )


def prune_empty_source_dirs(paths):
    for value in sorted({str(Path(path).parent) for path in paths}, reverse=True):
        current = Path(value)
        while any(root in current.parents for root in ALLOWED_ROOTS):
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent


def remove_staged(staged, expectations):
    remaining = 0
    removed_by_inode = {}
    for source, destination in staged:
        try:
            expected = expectations.get(str(source))
            if not isinstance(expected, dict):
                remaining += 1
                continue
            inode_key = (
                int(expected.get("dev") or -1),
                int(expected.get("inode") or -1),
            )
            already_removed = removed_by_inode.get(inode_key, 0)
            expected_nlink = (
                int(expected.get("nlink") or -1) - already_removed
            )
            if expected_nlink <= 0 or not validate_recovery_file(
                destination,
                expected,
                expected_nlink=expected_nlink,
            ):
                remaining += 1
                continue
            destination.unlink()
            removed_by_inode[inode_key] = already_removed + 1
        except Exception:
            remaining += 1
    remove_empty_quarantine_roots(staged)
    return remaining


def rollback_before_commit(
    staged,
    expectations,
    current,
    initially_running,
    backup_dir,
    moviepilot_rows=None,
):
    rollback_failures = (
        rollback_staged(staged, expectations) if staged else 0
    )
    remove_empty_quarantine_roots(staged)
    quarantine_residual = not quarantine_roots_clean(staged)
    restore_failed = False
    try:
        restore_run_states(current, initially_running)
    except Exception:
        restore_failed = True
    try:
        restore_moviepilot_indexes(moviepilot_rows or [])
    except Exception:
        restore_failed = True
    try:
        write_transaction_state(
            backup_dir,
            "rollback_incomplete"
            if rollback_failures or quarantine_residual or restore_failed
            else "rolled_back",
        )
    except Exception:
        restore_failed = True
    if rollback_failures or quarantine_residual or restore_failed:
        fail(
            "rollback_incomplete",
            "执行失败且文件或 qB 运行态未能完整恢复，请立即人工核对。",
        )


def read_regular_json(path, code, message):
    if path.is_symlink() or not path.is_file():
        fail(code, message)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        fail(code, message)
    if not isinstance(value, dict):
        fail(code, message)
    return value


def validate_recovery_file(path, expected, *, expected_nlink=None):
    try:
        lstat = path.lstat()
        current = path.stat()
    except FileNotFoundError:
        return False
    except OSError:
        fail("recovery_file_unreadable", "无法读取待恢复文件。")
    if stat.S_ISLNK(lstat.st_mode) or not stat.S_ISREG(current.st_mode):
        fail("recovery_file_unsafe", "待恢复目标不是可信普通文件。")
    actual = {
        "dev": int(current.st_dev),
        "inode": int(current.st_ino),
        "size": int(current.st_size),
        "nlink": int(current.st_nlink),
    }
    expected_values = {
        key: int(expected.get(key) or -1)
        for key in actual
    }
    if expected_nlink is not None:
        expected_values["nlink"] = int(expected_nlink)
    if any(expected_values[key] != actual[key] for key in actual):
        fail("recovery_file_changed", "待恢复文件状态已经变化。")
    return True


def ensure_restore_parent(source):
    matching_roots = [
        root
        for root in ALLOWED_ROOTS
        if source == root or root in source.parents
    ]
    if not matching_roots:
        fail("recovery_path_outside_allowlist", "恢复路径不在允许目录内。")
    root = max(matching_roots, key=lambda item: len(item.parts))
    if root.is_symlink() or not root.is_dir():
        fail("recovery_parent_unsafe", "恢复根目录不可信。")
    current = root
    for part in source.parent.relative_to(root).parts:
        current = current / part
        if current.exists():
            if current.is_symlink() or not current.is_dir():
                fail("recovery_parent_unsafe", "恢复父目录不可信。")
        else:
            current.mkdir()


def load_recovery_transaction(plan_id):
    backup_dir = EXECUTION_BACKUP / plan_id
    if (
        EXECUTION_BACKUP.is_symlink()
        or not EXECUTION_BACKUP.is_dir()
        or backup_dir.is_symlink()
        or not backup_dir.is_dir()
        or backup_dir.parent != EXECUTION_BACKUP
    ):
        fail("recovery_not_found", "找不到可信的事务备份。")
    transaction = read_regular_json(
        backup_dir / "transaction.json",
        "recovery_state_invalid",
        "事务状态无法验证。",
    )
    manifest = read_regular_json(
        backup_dir / "manifest.json",
        "recovery_manifest_invalid",
        "qB 恢复清单无法验证。",
    )
    if (
        transaction.get("planId") != plan_id
        or manifest.get("planId") != plan_id
    ):
        fail("recovery_plan_mismatch", "事务标识与备份不一致。")
    raw_records = transaction.get("stagedFiles") or []
    if not isinstance(raw_records, list):
        fail("recovery_state_invalid", "事务文件映射格式不正确。")
    records = []
    for index, raw in enumerate(raw_records):
        if not isinstance(raw, dict):
            fail("recovery_state_invalid", "事务文件映射格式不正确。")
        source_text = str(raw.get("source") or "")
        quarantine_text = str(raw.get("quarantine") or "")
        source = Path(source_text)
        quarantine = Path(quarantine_text)
        if not path_allowed(source_text):
            fail("recovery_path_outside_allowlist", "恢复路径不在允许目录内。")
        root = quarantine_root(source, plan_id)
        if root.parent.is_symlink() or root.is_symlink():
            fail("recovery_mapping_invalid", "事务隔离目录不可信。")
        token = hashlib.sha256(source_text.encode("utf-8")).hexdigest()[:16]
        expected_quarantine = root / f"{index:06d}-{token}"
        if quarantine != expected_quarantine:
            fail("recovery_mapping_invalid", "事务隔离路径无法验证。")
        expected = {
            key: int(raw.get(key) or -1)
            for key in ("dev", "inode", "size", "nlink")
        }
        if any(value < 0 for value in expected.values()):
            fail("recovery_mapping_invalid", "事务文件校验信息不完整。")
        records.append((source, quarantine, expected))
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list):
        fail("recovery_manifest_invalid", "qB 恢复清单格式不正确。")
    if not tasks and transaction.get("mode") != "delete":
        fail("recovery_manifest_invalid", "qB 恢复清单没有任务。")
    task_map = {}
    for item in tasks:
        if not isinstance(item, dict):
            fail("recovery_manifest_invalid", "qB 恢复清单格式不正确。")
        task_hash = str(item.get("hash") or "").lower()
        if not HASH_RE.fullmatch(task_hash) or task_hash in task_map:
            fail("recovery_manifest_invalid", "qB 恢复任务标识无效。")
        task_map[task_hash] = item
    expected_indexes = transaction.get("moviepilotIndexExpected") or []
    if not isinstance(expected_indexes, list):
        fail("recovery_state_invalid", "MoviePilot 索引预期记录格式不正确。")
    normalized_indexes = [
        moviepilot_expected_index(item) for item in expected_indexes
    ]
    index_rows = moviepilot_indexes_from_transaction(transaction)
    return (
        backup_dir,
        transaction,
        records,
        task_map,
        normalized_indexes,
        index_rows,
    )


def recovery_file_states(records):
    raw_presence = []
    gone_by_inode = {}
    for source, quarantine, expected in records:
        source_present = source.exists() or source.is_symlink()
        quarantine_present = (
            quarantine.exists() or quarantine.is_symlink()
        )
        raw_presence.append((source_present, quarantine_present))
        if not source_present and not quarantine_present:
            inode_key = (expected["dev"], expected["inode"])
            gone_by_inode[inode_key] = gone_by_inode.get(inode_key, 0) + 1
    result = []
    for (
        source,
        quarantine,
        expected,
    ), (
        source_present,
        quarantine_present,
    ) in zip(records, raw_presence):
        inode_key = (expected["dev"], expected["inode"])
        expected_nlink = (
            expected["nlink"] - gone_by_inode.get(inode_key, 0)
        )
        if expected_nlink < 0:
            fail("recovery_file_changed", "待恢复硬链接计数无效。")
        source_exists = (
            validate_recovery_file(
                source,
                expected,
                expected_nlink=expected_nlink,
            )
            if source_present
            else False
        )
        quarantine_exists = validate_recovery_file(
            quarantine,
            expected,
            expected_nlink=expected_nlink,
        ) if quarantine_present else False
        if source_exists and quarantine_exists:
            fail("recovery_duplicate_file", "原路径与隔离区同时存在同一文件。")
        result.append(
            {
                "sourceExists": source_exists,
                "quarantineExists": quarantine_exists,
            }
        )
    return result


def recover_transaction(payload):
    plan_id = str(payload.get("planId") or "")
    action = str(payload.get("action") or "inspect")
    phrase = str(payload.get("confirmPhrase") or "")
    if not PLAN_RE.fullmatch(plan_id):
        fail("invalid_plan_id", "恢复事务标识无效。")
    if action not in {"inspect", "rollback", "finalize"}:
        fail("invalid_recovery_action", "恢复动作无效。")
    (
        backup_dir,
        transaction,
        records,
        task_map,
        expected_indexes,
        index_rows,
    ) = load_recovery_transaction(plan_id)
    file_states = recovery_file_states(records)
    current = all_torrents()
    present_hashes = [
        task_hash for task_hash in task_map if task_hash in current
    ]
    absent_hashes = [
        task_hash for task_hash in task_map if task_hash not in current
    ]
    summary = {
        "ok": True,
        "planId": plan_id,
        "phase": str(transaction.get("phase") or ""),
        "mode": str(transaction.get("mode") or ""),
        "taskCount": len(task_map),
        "tasksPresent": len(present_hashes),
        "tasksAbsent": len(absent_hashes),
        "filesAtSource": sum(
            item["sourceExists"] for item in file_states
        ),
        "filesQuarantined": sum(
            item["quarantineExists"] for item in file_states
        ),
        "filesAlreadyGone": sum(
            not item["sourceExists"] and not item["quarantineExists"]
            for item in file_states
        ),
        "moviepilotIndexesDeleted": len(index_rows),
        "rollbackPhrase": f"回滚事务 {plan_id}",
        "finalizePhrase": f"完成事务 {plan_id}",
    }
    if action == "inspect":
        return summary
    if transaction.get("phase") in {"complete", "rolled_back"}:
        fail("recovery_already_terminal", "事务已经处于终态。")

    if action == "rollback":
        if phrase != summary["rollbackPhrase"]:
            fail("confirmation_mismatch", "恢复确认短语不完全一致。")
        if absent_hashes:
            fail(
                "recovery_qb_task_absent",
                "至少一个 qB 任务已经退出，不能自动回滚。",
            )
        if any(
            not state["sourceExists"]
            and not state["quarantineExists"]
            for state in file_states
        ):
            fail(
                "recovery_file_missing",
                "至少一个恢复文件已经不在原路径或隔离区。",
            )
        for (source, quarantine, expected), state in zip(
            records,
            file_states,
        ):
            if state["sourceExists"]:
                continue
            if not state["quarantineExists"]:
                fail("recovery_file_missing", "恢复文件已经不在原路径或隔离区。")
            validate_recovery_file(quarantine, expected)
            ensure_restore_parent(source)
            os.rename(quarantine, source)
        initially_running = [
            task_hash
            for task_hash, task in task_map.items()
            if not is_stopped(task.get("state"))
        ]
        restore_tasks = {
            task_hash: {
                "force_start": bool(task.get("forceStart")),
            }
            for task_hash, task in task_map.items()
        }
        restore_run_states(restore_tasks, initially_running)
        restore_moviepilot_indexes(index_rows)
        remove_empty_quarantine_roots(
            [(source, quarantine) for source, quarantine, _ in records]
        )
        if not quarantine_roots_clean(
            [(source, quarantine) for source, quarantine, _ in records]
        ):
            write_transaction_state(
                backup_dir,
                "rollback_incomplete",
                detail="quarantine directory is not empty",
            )
            fail(
                "recovery_quarantine_not_empty",
                "隔离目录仍含无法验证的内容，事务保持锁定。",
            )
        write_transaction_state(backup_dir, "rolled_back")
        return {**summary, "phase": "rolled_back", "resolved": True}

    if phrase != summary["finalizePhrase"]:
        fail("confirmation_mismatch", "恢复确认短语不完全一致。")
    if present_hashes:
        fail(
            "recovery_qb_task_present",
            "至少一个 qB 任务仍存在，不能完成删除。",
        )
    if transaction.get("mode") not in {"delete", "retire"}:
        fail("recovery_mode_invalid", "事务档位无法验证。")
    if transaction.get("mode") == "delete":
        if any(state["sourceExists"] for state in file_states):
            fail(
                "recovery_source_present",
                "至少一个文件仍在原路径，不能完成删除。",
            )
        if expected_indexes:
            remove_moviepilot_indexes(expected_indexes, backup_dir)
        gone_by_inode = {}
        for (_, _, expected), state in zip(records, file_states):
            if (
                not state["sourceExists"]
                and not state["quarantineExists"]
            ):
                inode_key = (expected["dev"], expected["inode"])
                gone_by_inode[inode_key] = (
                    gone_by_inode.get(inode_key, 0) + 1
                )
        for (_, quarantine, expected), state in zip(
            records,
            file_states,
        ):
            if state["quarantineExists"]:
                inode_key = (expected["dev"], expected["inode"])
                already_gone = gone_by_inode.get(inode_key, 0)
                expected_nlink = expected["nlink"] - already_gone
                validate_recovery_file(
                    quarantine,
                    expected,
                    expected_nlink=expected_nlink,
                )
                quarantine.unlink()
                gone_by_inode[inode_key] = already_gone + 1
        remove_empty_quarantine_roots(
            [(source, quarantine) for source, quarantine, _ in records]
        )
    write_transaction_state(backup_dir, "complete")
    return {**summary, "phase": "complete", "resolved": True}


def execute(payload):
    plan_id = str(payload.get("planId") or "")
    mode = str(payload.get("mode") or "")
    operations = payload.get("operations") or {}
    expectations = payload.get("fileExpectations") or {}
    if not PLAN_RE.fullmatch(plan_id):
        fail("invalid_plan_id", "执行计划标识无效。")
    if mode not in {"pause", "retire", "delete"}:
        fail("invalid_mode", "执行档位无效。")
    if unresolved_transactions():
        fail(
            "unresolved_transaction",
            "发现上一次未完成的清理事务，已锁定全部新操作。",
        )

    stop_hashes = validate_hashes(operations.get("qbStop") or [])
    remove_hashes = validate_hashes(
        operations.get("qbRemoveKeepFiles") or []
    )
    if mode == "pause" and (not stop_hashes or remove_hashes):
        fail("invalid_operations", "停止做种计划的任务操作不完整。")
    if mode == "retire" and (stop_hashes or not remove_hashes):
        fail("invalid_operations", "退出做种计划的任务操作不完整。")
    if mode == "delete" and stop_hashes:
        fail("invalid_operations", "完整删除计划包含错误的停止操作。")
    hashes = stop_hashes if mode == "pause" else remove_hashes
    raw_paths = operations.get("unlinkFiles") or []
    raw_moviepilot_indexes = operations.get("moviepilotIndexes") or []
    if not isinstance(raw_moviepilot_indexes, list):
        fail("moviepilot_index_invalid", "执行计划包含无效的 MoviePilot 索引。")
    if mode != "delete" and raw_moviepilot_indexes:
        fail("unexpected_moviepilot_indexes", "非删除档位不能包含 MoviePilot 索引操作。")
    expected_moviepilot_indexes = [
        moviepilot_expected_index(item) for item in raw_moviepilot_indexes
    ]
    if not isinstance(raw_paths, list) or any(
        not isinstance(path, str) or not path for path in raw_paths
    ):
        fail("invalid_file_path", "执行计划包含无效文件路径。")
    paths = list(raw_paths)
    if len(paths) != len(set(paths)):
        fail("duplicate_file_path", "执行计划包含重复文件。")
    if mode == "delete":
        if not paths:
            fail("empty_delete", "完整删除计划没有文件。")
        validate_files(paths, expectations)
    elif paths:
        fail("unexpected_files", "非删除档位不能包含文件操作。")

    current = all_torrents()
    missing = [task_hash for task_hash in hashes if task_hash not in current]
    if missing:
        fail("qb_task_missing", "qB 任务状态已变化，请重新生成计划。")
    if any(float(current[value].get("progress") or 0) < 0.999999 for value in hashes):
        fail("qb_task_unfinished", "qB 任务不再完整，已拒绝执行。")
    initially_running = [
        value
        for value in hashes
        if not is_stopped(current[value].get("state"))
    ]

    backup_dir = None
    moviepilot_rows = []
    if mode in {"retire", "delete"}:
        backup_dir = backup_qb_state(
            plan_id,
            hashes,
            current,
            copy_files=False,
        )
        write_transaction_state(
            backup_dir,
            "preparing",
            fields={
                "planId": plan_id,
                "mode": mode,
                "moviepilotIndexExpected": expected_moviepilot_indexes,
            },
            reset=True,
        )

    if hashes:
        try:
            qb_post("/api/v2/torrents/stop", {"hashes": "|".join(hashes)})
            wait_for_stopped(hashes)
        except Exception:
            if backup_dir is not None:
                rollback_before_commit(
                    [],
                    expectations,
                    current,
                    initially_running,
                    backup_dir,
                    moviepilot_rows,
                )
            else:
                try:
                    restore_run_states(current, initially_running)
                except Exception:
                    fail(
                        "rollback_incomplete",
                        "停止失败且原运行态未能完整恢复，请立即人工核对。",
                    )
            raise

    staged = []
    try:
        if mode in {"retire", "delete"}:
            backup_qb_state(
                plan_id,
                hashes,
                current,
                copy_files=True,
            )
            write_transaction_state(
                backup_dir,
                "prepared",
                fields={"planId": plan_id, "mode": mode},
            )
        if mode == "delete":
            validate_files(paths, expectations)
            if open_handle_count(paths):
                fail("file_in_use", "至少一个文件仍被播放、传输或其他进程占用。")
            write_transaction_state(
                backup_dir,
                "staging",
                fields={
                    "stagedFiles": staging_manifest(
                        paths,
                        plan_id,
                        expectations,
                    )
                },
            )
            stage_files(paths, plan_id, expectations, staged)
            write_transaction_state(
                backup_dir,
                "files_staged",
                staged=staged,
            )
    except Exception:
        rollback_before_commit(
            staged,
            expectations,
            current,
            initially_running,
            backup_dir,
            moviepilot_rows,
        )
        raise

    if remove_hashes:
        delete_error = None
        try:
            qb_post(
                "/api/v2/torrents/delete",
                {
                    "hashes": "|".join(remove_hashes),
                    "deleteFiles": "false",
                },
            )
        except Exception as exc:
            delete_error = exc
        removal_state = reconcile_removal(remove_hashes)
        if removal_state == "present":
            rollback_before_commit(
                staged,
                expectations,
                current,
                initially_running,
                backup_dir,
                moviepilot_rows,
            )
            if delete_error is not None:
                fail(
                    "qb_remove_failed",
                    "qB 拒绝退出任务，文件与运行态已经恢复。",
                )
            fail(
                "qb_remove_not_applied",
                "qB 未退出任务，文件与运行态已经恢复。",
            )
        if removal_state == "unknown":
            write_transaction_state(
                backup_dir,
                "uncertain",
                detail="qB delete outcome could not be read back",
            )
            fail(
                "qb_state_uncertain",
                "无法确认 qB 任务是否退出；文件未删除并保留在安全隔离区，"
                "qB 恢复备份也已保留，禁止继续执行。",
            )
        write_transaction_state(backup_dir, "qb_removed")

    if mode == "delete":
        try:
            moviepilot_rows = remove_moviepilot_indexes(
                expected_moviepilot_indexes,
                backup_dir,
            )
            write_transaction_state(
                backup_dir,
                "moviepilot_indexes_removed",
                fields={"moviepilotIndexRows": moviepilot_rows},
            )
        except Exception:
            rollback_before_commit(
                staged,
                expectations,
                current,
                initially_running,
                backup_dir,
                moviepilot_rows,
            )
            raise
        remaining = remove_staged(staged, expectations)
        prune_empty_source_dirs(paths)
        if remaining:
            write_transaction_state(
                backup_dir,
                "cleanup_incomplete",
                detail=f"{remaining} quarantined files remain",
            )
            fail(
                "quarantine_cleanup_incomplete",
                "资源已退出并移入隔离区，但部分文件尚未释放空间。",
            )
    write_transaction_state(backup_dir, "complete")

    return {
        "ok": True,
        "planId": plan_id,
        "mode": mode,
        "qbStopped": len(stop_hashes) if mode == "pause" else 0,
        "qbRemoved": len(remove_hashes),
        "filesDeleted": len(paths) if mode == "delete" else 0,
        "moviepilotIndexesDeleted": len(moviepilot_rows),
        "backupCreated": backup_dir is not None,
    }


try:
    payload = json.load(sys.stdin)
    result = (
        recover_transaction(payload)
        if payload.get("command") == "recover"
        else execute(payload)
    )
    print(json.dumps(result, ensure_ascii=False))
except RemoteFailure as exc:
    print(
        json.dumps(
            {
                "ok": False,
                "error": {"code": exc.code, "message": exc.message},
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(2)
except Exception:
    print(
        json.dumps(
            {
                "ok": False,
                "error": {
                    "code": "remote_internal_error",
                    "message": "NAS 执行器发生未预期错误，已停止。",
                },
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(3)
'''


class ExecutionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _encoded_executor(config: dict[str, Any]) -> str:
    source = REMOTE_EXECUTOR.replace(
        'globals().get("__PINAS_CONFIG__", {})',
        repr(config),
        1,
    )
    return base64.b64encode(source.encode("utf-8")).decode("ascii")


def validate_confirmation(
    plan: dict[str, Any],
    *,
    confirm_phrase: str,
    now: datetime | None = None,
) -> None:
    if not plan.get("canExecute"):
        raise ExecutionError("plan_blocked", "安全预演未通过，禁止执行。")
    expected = str(plan.get("confirmPhrase") or "")
    if not expected or not hmac.compare_digest(
        confirm_phrase.encode("utf-8"),
        expected.encode("utf-8"),
    ):
        raise ExecutionError("confirmation_mismatch", "确认短语不完全一致。")
    try:
        expires_at = datetime.fromisoformat(str(plan["expiresAt"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ExecutionError("invalid_plan_expiry", "执行计划缺少有效期限。") from exc
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if expires_at.astimezone(timezone.utc) <= current:
        raise ExecutionError("plan_expired", "安全预演已经过期，请重新生成。")


class SSHExecutionRunner:
    def __init__(
        self,
        *,
        host: str | None = None,
        config: dict[str, Any] | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]]
        | None = None,
    ):
        self.config = config or default_config()
        self.host = host or self.config["ssh_host"]
        self.command_runner = command_runner or subprocess.run

    def __call__(self, plan: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "planId": plan["planId"],
            "mode": plan["mode"],
            "operations": plan["operations"],
            "fileExpectations": plan["fileExpectations"],
        }
        encoded_executor = _encoded_executor(self.config)
        python_code = (
            "import base64;"
            f"exec(base64.b64decode({encoded_executor!r}))"
        )
        remote_command = (
            "sudo -n /usr/bin/python3 -c "
            + shlex.quote(python_code)
        )
        try:
            completed = self.command_runner(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    self.host,
                    remote_command,
                ],
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExecutionError(
                "remote_unavailable",
                "无法连接 NAS 执行器，未确认任何操作完成。",
            ) from exc
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ExecutionError(
                "invalid_remote_response",
                "NAS 执行器没有返回可验证结果。",
            ) from exc
        if completed.returncode != 0 or not response.get("ok"):
            error = response.get("error") or {}
            raise ExecutionError(
                str(error.get("code") or "remote_execution_failed"),
                str(error.get("message") or "NAS 执行失败。"),
            )
        return response


class SSHRecoveryRunner:
    def __init__(
        self,
        *,
        host: str | None = None,
        config: dict[str, Any] | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]]
        | None = None,
    ):
        self.config = config or default_config()
        self.host = host or self.config["ssh_host"]
        self.command_runner = command_runner or subprocess.run

    def __call__(
        self,
        *,
        plan_id: str,
        action: str = "inspect",
        confirm_phrase: str = "",
    ) -> dict[str, Any]:
        payload = {
            "command": "recover",
            "planId": plan_id,
            "action": action,
            "confirmPhrase": confirm_phrase,
        }
        encoded_executor = _encoded_executor(self.config)
        python_code = (
            "import base64;"
            f"exec(base64.b64decode({encoded_executor!r}))"
        )
        remote_command = (
            "sudo -n /usr/bin/python3 -c "
            + shlex.quote(python_code)
        )
        try:
            completed = self.command_runner(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    self.host,
                    remote_command,
                ],
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExecutionError(
                "remote_unavailable",
                "无法连接 NAS 恢复器，未确认任何恢复动作完成。",
            ) from exc
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ExecutionError(
                "invalid_remote_response",
                "NAS 恢复器没有返回可验证结果。",
            ) from exc
        if completed.returncode != 0 or not response.get("ok"):
            error = response.get("error") or {}
            raise ExecutionError(
                str(error.get("code") or "remote_recovery_failed"),
                str(error.get("message") or "NAS 事务恢复失败。"),
            )
        return response


class LocalExecutionRunner:
    """Run the isolated executor directly on the Pi via passwordless sudo."""

    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]]
        | None = None,
    ):
        self.config = config or default_config()
        self.command_runner = command_runner or subprocess.run

    def __call__(self, plan: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "planId": plan["planId"],
            "mode": plan["mode"],
            "operations": plan["operations"],
            "fileExpectations": plan["fileExpectations"],
        }
        return _run_local_executor(
            payload,
            config=self.config,
            command_runner=self.command_runner,
            unavailable_message="NAS 本机执行器不可用，未确认任何操作完成。",
            failure_code="local_execution_failed",
            failure_message="NAS 本机执行失败。",
        )


class LocalRecoveryRunner:
    """Inspect or recover an isolated cleanup transaction directly on the Pi."""

    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]]
        | None = None,
    ):
        self.config = config or default_config()
        self.command_runner = command_runner or subprocess.run

    def __call__(
        self,
        *,
        plan_id: str,
        action: str = "inspect",
        confirm_phrase: str = "",
    ) -> dict[str, Any]:
        return _run_local_executor(
            {
                "command": "recover",
                "planId": plan_id,
                "action": action,
                "confirmPhrase": confirm_phrase,
            },
            config=self.config,
            command_runner=self.command_runner,
            unavailable_message="NAS 本机恢复器不可用，未确认任何恢复动作完成。",
            failure_code="local_recovery_failed",
            failure_message="NAS 本机事务恢复失败。",
        )


def _run_local_executor(
    payload: dict[str, Any],
    *,
    config: dict[str, Any],
    command_runner: Callable[..., subprocess.CompletedProcess[str]],
    unavailable_message: str,
    failure_code: str,
    failure_message: str,
) -> dict[str, Any]:
    encoded_executor = _encoded_executor(config)
    python_code = (
        "import base64;"
        f"exec(base64.b64decode({encoded_executor!r}))"
    )
    command = ["sudo", "-n", "/usr/bin/python3", "-c", python_code]
    try:
        completed = command_runner(
            command,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExecutionError(
            "local_unavailable",
            unavailable_message,
        ) from exc
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ExecutionError(
            "invalid_local_response",
            "NAS 本机执行器没有返回可验证结果。",
        ) from exc
    if completed.returncode != 0 or not response.get("ok"):
        error = response.get("error") or {}
        raise ExecutionError(
            str(error.get("code") or failure_code),
            str(error.get("message") or failure_message),
        )
    return response
