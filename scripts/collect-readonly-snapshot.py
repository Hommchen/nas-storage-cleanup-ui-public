#!/opt/homebrew/bin/python3.12
"""Collect a sanitized, read-only PiNAS resource snapshot over SSH."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

try:
    from media_metadata import (
        apply_metadata_overrides,
        annotate_hr_missing_resources,
        enrich_and_merge_resources,
        make_hr_metadata_resources,
        prune_metadata_cache,
        resolve_media_names,
        sanitize_metadata_cache,
    )
    from snapshot_integrity import validate_snapshot_pair
except ModuleNotFoundError:
    from scripts.media_metadata import (
        apply_metadata_overrides,
        annotate_hr_missing_resources,
        enrich_and_merge_resources,
        make_hr_metadata_resources,
        prune_metadata_cache,
        resolve_media_names,
        sanitize_metadata_cache,
    )
    from scripts.snapshot_integrity import validate_snapshot_pair


REMOTE_COLLECTOR = r'''
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import difflib
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

JELLYFIN_DB = "/var/lib/jellyfin/data/jellyfin.db"
MOVIEPILOT_DB = "/mnt/sdc/library-tools/moviepilot/config/user.db"
QB_URL = "http://127.0.0.1:8080"
EXECUTION_BACKUP = Path("/mnt/sdc/library-tools/storage-cleanup/qb-backups")
QUARANTINE_ROOTS = (
    Path("/mnt/sdc/.storage-cleanup-quarantine"),
    Path("/mnt/sdd/.storage-cleanup-quarantine"),
)
MOVIE = "MediaBrowser.Controller.Entities.Movies.Movie"
SERIES = "MediaBrowser.Controller.Entities.TV.Series"
EPISODE = "MediaBrowser.Controller.Entities.TV.Episode"
VIDEO_EXTENSIONS = {".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".ts", ".webm"}
SEASON_RE = re.compile(r"(?i)(?:^|[ ._\-/])S(?:eason[ ._-]*)?(\d{1,2})(?:[ ._\-/]|$)")
ALLOWED_ROOTS = (
    "/mnt/sdc/downloads/completed",
    "/mnt/sdd/downloads/completed",
    "/mnt/sdc/.media-main/Movies",
    "/mnt/sdd/media/TV",
    "/mnt/sdc/media/Movies",
    "/mnt/sdc/media/TV",
    "/mnt/sdc/.media-quarantine",
    "/mnt/sdd/.media-quarantine",
)
HARDLINK_DISCOVERY_ROOTS = ALLOWED_ROOTS
HR_HASH_CACHE = __HR_HASH_CACHE__
QB_FILE_CACHE = __QB_FILE_CACHE__


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
    for root in QUARANTINE_ROOTS:
        if not root.is_dir() or root.is_symlink():
            continue
        for child in root.iterdir():
            unresolved.add(child.name)
    return sorted(unresolved)


def moviepilot_media_index_rows():
    """Read MoviePilot's Jellyfin index so delete plans can close both stores."""

    required = {
        "id",
        "server",
        "item_id",
        "item_type",
        "title",
        "original_title",
        "year",
        "path",
        "seasoninfo",
    }
    try:
        connection = sqlite3.connect(
            f"file:{MOVIEPILOT_DB}?mode=ro",
            uri=True,
            timeout=10,
        )
        connection.row_factory = sqlite3.Row
        columns = {
            str(row[1])
            for row in connection.execute(
                "pragma table_info('mediaserveritem')"
            )
        }
        if not required.issubset(columns):
            connection.close()
            return [], False
        rows = connection.execute(
            "select id,server,item_id,item_type,title,original_title,year,path,seasoninfo "
            "from mediaserveritem where server='jellyfin' order by id"
        ).fetchall()
        connection.close()
    except (OSError, sqlite3.Error):
        return [], False
    return [
        {
            "id": int(row["id"]),
            "server": str(row["server"] or ""),
            "itemId": str(row["item_id"] or ""),
            "itemType": str(row["item_type"] or ""),
            "title": str(row["title"] or ""),
            "originalTitle": str(row["original_title"] or ""),
            "year": str(row["year"] or ""),
            "path": str(row["path"] or ""),
            "seasonInfo": str(row["seasoninfo"] or ""),
        }
        for row in rows
    ], True


def qb_json(path, timeout=45):
    with urlopen(QB_URL + path, timeout=timeout) as response:
        return json.load(response)


def has_cjk(value):
    return any("\u3400" <= char <= "\u9fff" for char in str(value or ""))


def looks_like_infohash(value):
    return bool(re.fullmatch(r"[0-9a-fA-F]{40}", str(value or "").strip()))


def first_nonempty(values, predicate=lambda value: True):
    for value in values:
        text = str(value or "").strip()
        if text and predicate(text):
            return text
    return ""


def stable_resource_id(value):
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:20]
    return f"res_{digest}"


def is_allowed_path(value):
    path = os.path.realpath(str(value or ""))
    return any(path == root or path.startswith(root + "/") for root in ALLOWED_ROOTS)


def identity(item_id, providers, media_type, path):
    values = providers.get(item_id, {})
    for provider in ("Tmdb", "Tvdb", "Imdb"):
        if values.get(provider):
            return f"{media_type}:{provider.lower()}:{values[provider]}"
    return f"{media_type}:path:{str(path or item_id).casefold()}"


def video_files(path_value):
    path = Path(str(path_value or ""))
    if not path.exists():
        return []
    if path.is_file():
        return [path] if path.suffix.lower() in VIDEO_EXTENSIONS else []
    result = []
    try:
        for root, _, files in os.walk(path):
            for name in files:
                candidate = Path(root, name)
                if candidate.suffix.lower() in VIDEO_EXTENSIONS:
                    result.append(candidate)
    except OSError:
        return []
    return result


def all_regular_files(path_value):
    path = Path(str(path_value or ""))
    if not path.exists():
        return []
    if path.is_file():
        return [path]
    result = []
    for root, _, files in os.walk(path, onerror=lambda error: (_ for _ in ()).throw(error)):
        for name in files:
            candidate = Path(root, name)
            if candidate.is_file():
                result.append(candidate)
    return result


def hardlink_path_index():
    paths_by_inode = defaultdict(set)
    verified = True
    for root_value in HARDLINK_DISCOVERY_ROOTS:
        root = Path(root_value)
        if not root.exists():
            continue
        if root.is_symlink() or not root.is_dir():
            verified = False
            continue

        def mark_error(_error):
            nonlocal verified
            verified = False

        try:
            for current_root, directories, files in os.walk(
                root,
                topdown=True,
                followlinks=False,
                onerror=mark_error,
            ):
                current = Path(current_root)
                safe_directories = []
                for name in directories:
                    candidate = current / name
                    try:
                        if not candidate.is_symlink():
                            safe_directories.append(name)
                    except OSError:
                        verified = False
                directories[:] = safe_directories
                for name in files:
                    candidate = current / name
                    try:
                        lstat = candidate.lstat()
                        if candidate.is_symlink() or not candidate.is_file():
                            continue
                        stat = candidate.stat()
                    except OSError:
                        verified = False
                        continue
                    if not os.path.isfile(candidate):
                        continue
                    inode = (int(stat.st_dev), int(stat.st_ino))
                    paths_by_inode[inode].add(str(candidate))
        except OSError:
            verified = False
    return paths_by_inode, verified


def exact_qb_files(row):
    task_hash = str(row.get("hash") or "")
    if not task_hash:
        return [], False, None, False
    files = None
    from_cache = False
    cached = QB_FILE_CACHE.get(task_hash.lower())
    if float(row.get("progress") or 0) >= 0.999999 and isinstance(cached, list):
        files = cached
        from_cache = True
    if files is None:
        try:
            files = qb_json(
                "/api/v2/torrents/files?hash=" + quote(task_hash, safe=""),
                timeout=15,
            )
        except Exception:
            return [], False, None, False
    save_path = Path(str(row.get("save_path") or ""))
    result = []
    cache_entry = []
    for item in files:
        name = str(item.get("name") or "")
        relative = Path(name)
        if not name:
            continue
        size = int(item.get("size") or 0)
        progress = float(item.get("progress") or 0)
        result.append(
            {
                "path": str(save_path / relative),
                "name": name,
                "size": size,
                "progress": progress,
                "relativeSafe": not relative.is_absolute()
                and ".." not in relative.parts,
            }
        )
        cache_entry.append(
            {"name": name, "size": size, "progress": progress}
        )
    return result, True, cache_entry, from_cache


def inode_info(path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return (int(stat.st_dev), int(stat.st_ino)), {
        "size": int(stat.st_size),
        "nlink": int(stat.st_nlink),
        "path": str(path),
    }


def site_label(row):
    host = urlparse(str(row.get("tracker") or "")).hostname or ""
    haystack = " ".join(
        str(row.get(key) or "") for key in ("category", "tags", "tracker")
    ).casefold()
    mappings = (
        (("btschool", "ubits"), "学校站"),
        (("crabpt", "蟹黄堡"), "蟹黄堡"),
        (("oshen",), "OshenPT"),
        (("cyanbug",), "大青虫"),
        (("hdbao",), "红豆包"),
        (("daxiangjiao",), "大香蕉"),
    )
    for needles, label in mappings:
        if any(needle.casefold() in haystack or needle.casefold() in host.casefold() for needle in needles):
            return label
    return host or str(row.get("category") or "qB")


def task_status(row):
    tags = str(row.get("tags") or "")
    state = str(row.get("state") or "")
    if "pt-own-upload" in tags or "自发布" in tags:
        return "自发布", "warning"
    if state in {"downloading", "forcedDL", "stalledDL", "metaDL", "checkingDL"}:
        return "下载中", "protected"
    if state.startswith("stopped") or state == "pausedUP":
        return "已停止", "normal"
    return "做种中", "normal"


def normalized_release(value):
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", str(value or "").casefold())).strip()


def release_matches(left, right):
    if not left or not right:
        return False
    left_key = left.replace(" ", "")
    right_key = right.replace(" ", "")
    if left_key == right_key or left_key in right_key or right_key in left_key:
        return True
    ratio = difflib.SequenceMatcher(None, left_key, right_key).ratio()
    if ratio >= 0.80:
        return True
    release_tokens = set(re.findall(r"[a-z0-9]+", left.casefold()))
    title_tokens = []
    for token in re.findall(r"[a-z0-9]+", right.casefold()):
        if re.fullmatch(r"(?:19|20)\d{2}|s\d{1,2}(?:e\d{1,3})?|(?:720|1080|2160)p?", token):
            break
        title_tokens.append(token)
    meaningful = {
        token
        for token in title_tokens
        if token not in {"a", "an", "and", "in", "of", "the", "to", "with"}
    }
    return (
        len(title_tokens) >= 2
        and bool(meaningful)
        and set(title_tokens).issubset(release_tokens)
    )


def bencode_value_end(data, position):
    token = data[position : position + 1]
    if token == b"i":
        return data.index(b"e", position + 1) + 1
    if token in (b"l", b"d"):
        position += 1
        while data[position : position + 1] != b"e":
            position = bencode_value_end(data, position)
        return position + 1
    colon = data.index(b":", position)
    length = int(data[position:colon])
    return colon + 1 + length


def bencode_bytes(data, position):
    colon = data.index(b":", position)
    length = int(data[position:colon])
    start = colon + 1
    return data[start : start + length], start + length


def torrent_infohash(data):
    if data[:1] != b"d":
        raise ValueError("torrent is not a bencoded dictionary")
    position = 1
    while data[position : position + 1] != b"e":
        key, position = bencode_bytes(data, position)
        value_start = position
        position = bencode_value_end(data, position)
        if key == b"info":
            return hashlib.sha1(data[value_start:position]).hexdigest()
    raise ValueError("torrent has no info dictionary")


def btschool_hr_records(torrents):
    hash_cache = {
        str(torrent_id): str(task_hash).lower()
        for torrent_id, task_hash in HR_HASH_CACHE.items()
        if str(torrent_id).isdigit()
        and re.fullmatch(r"[0-9a-fA-F]{40}", str(task_hash))
    }
    try:
        site_db = sqlite3.connect(f"file:{MOVIEPILOT_DB}?mode=ro", uri=True)
        row = site_db.execute(
            "select url,cookie,ua from site "
            "where domain=? and is_active=1 order by id desc limit 1",
            ("btschool.club",),
        ).fetchone()
        site_db.close()
        if not row:
            return set(), False
        base_url, cookie, user_agent = row
        request = Request(
            str(base_url).rstrip("/") + "/myhr.php",
            headers={
                "Cookie": str(cookie or ""),
                "User-Agent": str(user_agent or "Mozilla/5.0"),
            },
        )
        with urlopen(request, timeout=25) as response:
            body = response.read().decode("utf-8", "ignore")
        records = {}
        pattern = r'<a[^>]+href=["\']([^"\']*details\.php\?id=\d+[^"\']*)["\'][^>]*>(.*?)</a>'
        for href, content in re.findall(pattern, body, re.I | re.S):
            match = re.search(r"(?:\?|&)id=(\d+)", href)
            title = html.unescape(re.sub(r"<[^>]+>", "", content)).strip()
            if not match or match.group(1) == "181845" or not title:
                continue
            records[match.group(1)] = title

        headers = {
            "Cookie": str(cookie or ""),
            "User-Agent": str(user_agent or "Mozilla/5.0"),
        }

        def official_record(item):
            torrent_id, title = item
            task_hash = hash_cache.get(torrent_id)
            if not task_hash:
                request = Request(
                    str(base_url).rstrip("/")
                    + "/download.php?id="
                    + torrent_id,
                    headers=headers,
                )
                with urlopen(request, timeout=30) as response:
                    payload = response.read()
                task_hash = torrent_infohash(payload)
                hash_cache[torrent_id] = task_hash
            return {
                "id": torrent_id,
                "title": title,
                "normalizedTitle": normalized_release(title),
                "hash": task_hash,
            }

        with ThreadPoolExecutor(max_workers=6) as executor:
            official_records = list(
                executor.map(official_record, sorted(records.items()))
            )
        qb_hashes = {
            str(row.get("hash") or "").lower()
            for row in torrents
            if row.get("hash")
        }
        exact_hashes = {
            record["hash"]
            for record in official_records
            if record["hash"] in qb_hashes
        }
        missing_records = [
            record
            for record in official_records
            if record["hash"] not in qb_hashes
        ]
        candidate_hashes, covered_titles = assign_hr_candidates(
            torrents,
            {
                record["normalizedTitle"]
                for record in missing_records
            },
        )
        return {
            "available": True,
            "activeCount": len(official_records),
            "exactHashes": exact_hashes,
            "missingCount": len(missing_records),
            "missingUncoveredCount": (
                len(missing_records) - len(covered_titles)
            ),
            "candidateHashes": candidate_hashes,
            "hashCache": hash_cache,
            "missingRecords": [
                {
                    "id": record["id"],
                    "title": record["title"],
                    "coveredByCandidate": (
                        record["normalizedTitle"] in covered_titles
                    ),
                }
                for record in missing_records
            ],
        }
    except Exception:
        return {
            "available": False,
            "activeCount": 0,
            "exactHashes": set(),
            "missingCount": 0,
            "missingUncoveredCount": 0,
            "candidateHashes": set(),
            "hashCache": hash_cache,
            "missingRecords": [],
        }


def assign_hr_candidates(torrents, hr_titles):
    assignments = set()
    covered_titles = set()
    for title in sorted(hr_titles):
        candidates = []
        for row in torrents:
            task_hash = str(row.get("hash") or "").lower()
            if not task_hash:
                continue
            release = normalized_release(row.get("name"))
            if not release_matches(release, title):
                continue
            score = difflib.SequenceMatcher(
                None,
                release.replace(" ", ""),
                title.replace(" ", ""),
            ).ratio()
            candidates.append((score, task_hash))
        if candidates:
            assignments.add(max(candidates)[1])
            covered_titles.add(title)
    return assignments, covered_titles


def make_task(
    row,
    scope,
    hr_hashes,
    hr_candidate_hashes,
    hr_available,
):
    site = site_label(row)
    status, tone = task_status(row)
    self_publish = status == "自发布"
    tags = str(row.get("tags") or "")
    site_hr = str(row.get("hash") or "").lower() in hr_hashes
    recovery_candidate = (
        str(row.get("hash") or "").lower() in hr_candidate_hashes
    )
    hr = "H&R" in tags or "H＆R" in tags or site_hr
    hr_unknown = recovery_candidate or (
        site == "学校站" and not hr_available
    )
    if hr:
        status, tone = "H&R 保护", "protected"
    elif hr_unknown:
        status, tone = "待核 H&R", "protected"
    return {
        "site": site,
        "scope": scope,
        "status": status,
        "tone": tone,
        "state": str(row.get("state") or ""),
        "brush": "刷流" in tags,
        "hr": hr,
        "hr_unknown": hr_unknown,
        "self_publish": self_publish,
        "progress": float(row.get("progress") or 0),
        "_hash": str(row.get("hash") or ""),
        "_name": str(row.get("name") or ""),
        "_private": bool(row.get("private")),
        "_content_path": str(row.get("content_path") or ""),
        "_save_path": str(row.get("save_path") or ""),
        "_category": str(row.get("category") or ""),
        "_tags": str(row.get("tags") or ""),
        "_exact_files": list(row.get("_exact_files") or []),
        "_file_list_verified": bool(row.get("_file_list_verified")),
    }


def season_label(seasons, all_seasons):
    values = sorted(value for value in seasons if value > 0)
    if not values:
        return "整部"
    if len(values) == 1:
        return f"S{values[0]:02d}"
    contiguous = values == list(range(values[0], values[-1] + 1))
    label = f"S{values[0]:02d}–S{values[-1]:02d}" if contiguous else "、".join(f"S{value:02d}" for value in values)
    if set(values) == set(all_seasons) and len(values) > 1:
        return label + " 全季合集"
    return label


def human_size(value):
    if value >= 1024 ** 4:
        return f"{value / 1024 ** 4:.2f} TB"
    return f"{value / 1024 ** 3:.1f} GB"


def private_record(
    identity_value,
    inodes,
    tasks,
    library_roots,
    all_links_known,
    moviepilot_indexes,
    moviepilot_index_source_available,
):
    files = []
    for inode in sorted(inodes):
        meta = inode_meta[inode]
        for path in sorted(known_paths[inode]):
            files.append(
                {
                    "path": path,
                    "dev": inode[0],
                    "inode": inode[1],
                    "size": meta["size"],
                    "nlink": meta["nlink"],
                    "allowed": is_allowed_path(path),
                }
            )
    qb_tasks = [
        {
            "hash": task["_hash"],
            "name": task["_name"],
            "site": task["site"],
            "scope": task["scope"],
            "state": task["state"],
            "progress": task["progress"],
            "private": task["_private"],
            "hr": task["hr"],
            "hrUnknown": task["hr_unknown"],
            "selfPublish": task["self_publish"],
            "contentPath": task["_content_path"],
            "savePath": task["_save_path"],
            "category": task["_category"],
            "tags": task["_tags"],
            "fileListVerified": task["_file_list_verified"],
            "exactFiles": task["_exact_files"],
        }
        for task in tasks
    ]
    cleanup_candidates = {}
    library_scan_verified = True
    for root in sorted({str(path) for path in library_roots if str(path)}):
        root_path = Path(root)
        if not root_path.exists():
            continue
        try:
            for path in all_regular_files(root_path):
                cleanup_candidates.setdefault(str(path), "library")
        except OSError:
            library_scan_verified = False
    for task in tasks:
        for item in task["_exact_files"]:
            cleanup_candidates.setdefault(str(item["path"]), "qb")
    for path in list(cleanup_candidates):
        info = inode_info(Path(path))
        if not info:
            continue
        inode, _ = info
        for linked_path in hardlink_paths.get(inode, ()):
            cleanup_candidates.setdefault(linked_path, "hardlink")

    cleanup_files = []
    cleanup_paths_by_inode = defaultdict(set)
    cleanup_expected_links = {}
    for path, source in sorted(cleanup_candidates.items()):
        record = {
            "path": path,
            "source": source,
            "allowed": is_allowed_path(path),
            "exists": False,
            "regular": False,
            "relativeSafe": True,
            "required": source in {"library", "hardlink"},
        }
        matching_qb_items = [
            item
            for task in tasks
            for item in task["_exact_files"]
            if item["path"] == path
        ]
        if matching_qb_items:
            record["relativeSafe"] = all(
                item["relativeSafe"] for item in matching_qb_items
            )
            record["qbExpectedSize"] = max(
                item["size"] for item in matching_qb_items
            )
            record["qbProgress"] = min(
                item["progress"] for item in matching_qb_items
            )
            record["required"] = record["required"] or any(
                item["progress"] >= 0.999999
                for item in matching_qb_items
            )
        try:
            candidate = Path(path)
            record["exists"] = candidate.exists()
            record["regular"] = candidate.is_file() and not candidate.is_symlink()
            if record["regular"]:
                stat = candidate.stat()
                record.update(
                    {
                        "dev": int(stat.st_dev),
                        "inode": int(stat.st_ino),
                        "size": int(stat.st_size),
                        "nlink": int(stat.st_nlink),
                    }
                )
                inode_key = (record["dev"], record["inode"])
                cleanup_paths_by_inode[inode_key].add(path)
                cleanup_expected_links[inode_key] = record["nlink"]
        except OSError:
            pass
        cleanup_files.append(record)

    existing_cleanup_files = [
        item for item in cleanup_files if item["exists"]
    ]
    cleanup_links_known = (
        hardlink_scan_verified
        and bool(existing_cleanup_files)
        and all(
            len(paths) == cleanup_expected_links[inode]
            and cleanup_expected_links[inode] > 0
            for inode, paths in cleanup_paths_by_inode.items()
        )
        and all(item["regular"] for item in existing_cleanup_files)
    )
    qb_file_lists_verified = all(
        task["_file_list_verified"] for task in tasks
    )
    root_records = [
        {"path": path, "allowed": is_allowed_path(path)}
        for path in sorted(
            {
                *{str(path) for path in library_roots if str(path)},
                *{
                    task["_content_path"]
                    for task in tasks
                    if task["_content_path"]
                },
            }
        )
    ]
    return {
        "identity": str(identity_value),
        "allLinksKnown": bool(all_links_known),
        "files": files,
        "cleanupFiles": cleanup_files,
        "cleanupLinksKnown": bool(cleanup_links_known),
        "libraryScanVerified": bool(library_scan_verified),
        "qbFileListsVerified": bool(qb_file_lists_verified),
        "roots": root_records,
        "qbTasks": qb_tasks,
        "moviepilotIndexes": moviepilot_indexes,
        "moviepilotIndexSourceAvailable": bool(
            moviepilot_index_source_available
        ),
    }


def library_cleanup_roots(group):
    group_video_paths = {str(path) for path in group["files"]}
    result = []
    for raw_path in group["paths"]:
        path = Path(str(raw_path))
        if group["media_type"] != "movie" or not path.is_file():
            result.append(path)
            continue
        parent = path.parent
        if not is_allowed_path(parent) or any(
            os.path.realpath(parent) == root for root in ALLOWED_ROOTS
        ):
            result.append(path)
            continue
        sibling_videos = {str(candidate) for candidate in video_files(parent)}
        if sibling_videos and sibling_videos.issubset(group_video_paths):
            result.append(parent)
        else:
            result.append(path)
    return result


db = sqlite3.connect(f"file:{JELLYFIN_DB}?mode=ro", uri=True)
db.row_factory = sqlite3.Row
providers = defaultdict(dict)
for row in db.execute("select ItemId, ProviderId, ProviderValue from BaseItemProviders"):
    providers[str(row["ItemId"])][str(row["ProviderId"])] = str(row["ProviderValue"])

top_items = list(
    db.execute(
        "select Id, Type, Name, OriginalTitle, ProductionYear, Path "
        "from BaseItems where Type in (?, ?) and IsVirtualItem=0",
        (MOVIE, SERIES),
    )
)
groups = {}
series_to_group = {}
for row in top_items:
    media_type = "movie" if row["Type"] == MOVIE else "tv"
    key = identity(str(row["Id"]), providers, media_type, row["Path"])
    group = groups.setdefault(
        key,
        {
            "key": key,
            "media_type": media_type,
            "names": [],
            "original_titles": [],
            "years": [],
            "paths": [],
            "series_ids": set(),
            "item_ids": set(),
            "files": [],
            "seasons": set(),
            "episode_count": 0,
        },
    )
    group["names"].append(row["Name"])
    group["item_ids"].add(str(row["Id"]))
    group["original_titles"].append(row["OriginalTitle"])
    if row["ProductionYear"]:
        group["years"].append(int(row["ProductionYear"]))
    if row["Path"]:
        group["paths"].append(str(row["Path"]))
    if media_type == "tv":
        group["series_ids"].add(str(row["Id"]))
        series_to_group[str(row["Id"])] = key
    else:
        group["files"].extend(video_files(row["Path"]))

for row in db.execute(
    "select SeriesId, Path, Size, ParentIndexNumber from BaseItems "
    "where Type=? and IsVirtualItem=0 and Path is not null",
    (EPISODE,),
):
    key = series_to_group.get(str(row["SeriesId"]))
    if not key:
        continue
    group = groups[key]
    path = Path(str(row["Path"]))
    group["files"].append(path)
    group["episode_count"] += 1
    season = row["ParentIndexNumber"]
    if season is None:
        match = SEASON_RE.search(str(path))
        season = int(match.group(1)) if match else None
    if season is not None:
        group["seasons"].add(int(season))

moviepilot_rows, moviepilot_index_source_available = moviepilot_media_index_rows()
moviepilot_by_item_id = defaultdict(list)
moviepilot_by_path = defaultdict(list)
for row in moviepilot_rows:
    if row["itemId"]:
        moviepilot_by_item_id[row["itemId"]].append(row)
    if row["path"]:
        moviepilot_by_path[row["path"]].append(row)
for group in groups.values():
    indexes = {}
    for item_id in group["item_ids"]:
        for row in moviepilot_by_item_id.get(item_id, []):
            indexes[row["id"]] = row
    for path in group["paths"]:
        for row in moviepilot_by_path.get(str(path), []):
            indexes[row["id"]] = row
    group["moviepilot_indexes"] = [
        indexes[key] for key in sorted(indexes)
    ]

inode_groups = defaultdict(set)
inode_meta = {}
inode_seasons = defaultdict(set)
known_paths = defaultdict(set)
for key, group in groups.items():
    unique_files = []
    seen_paths = set()
    for path in group["files"]:
        text = str(path)
        if text in seen_paths:
            continue
        seen_paths.add(text)
        unique_files.append(path)
        info = inode_info(path)
        if not info:
            continue
        inode, meta = info
        inode_groups[inode].add(key)
        inode_meta[inode] = meta
        known_paths[inode].add(text)
        season_match = SEASON_RE.search(text)
        if season_match:
            inode_seasons[inode].add(int(season_match.group(1)))
    group["files"] = unique_files

torrents = qb_json("/api/v2/torrents/info")
with ThreadPoolExecutor(max_workers=12) as executor:
    exact_file_results = list(executor.map(exact_qb_files, torrents))
next_qb_file_cache = {}
qb_file_lists_cached = 0
for row, (
    exact_files,
    file_list_verified,
    cache_entry,
    from_cache,
) in zip(
    torrents,
    exact_file_results,
):
    row["_exact_files"] = exact_files
    row["_file_list_verified"] = file_list_verified
    if (
        file_list_verified
        and float(row.get("progress") or 0) >= 0.999999
        and cache_entry is not None
    ):
        next_qb_file_cache[str(row.get("hash") or "").lower()] = cache_entry
    if from_cache:
        qb_file_lists_cached += 1
hr_status = btschool_hr_records(torrents)
hr_available = hr_status["available"]
hr_hashes = hr_status["exactHashes"]
hr_candidate_hashes = hr_status["candidateHashes"]
group_tasks = defaultdict(list)
unmatched_groups = {}
unmatched_tasks = 0
for row in torrents:
    task_inodes = set()
    task_paths = []
    exact_video_paths = [
        Path(item["path"])
        for item in row.get("_exact_files") or []
        if Path(item["path"]).suffix.lower() in VIDEO_EXTENSIONS
    ]
    candidate_video_paths = (
        exact_video_paths
        if row.get("_file_list_verified")
        else video_files(row.get("content_path"))
    )
    for path in candidate_video_paths:
        info = inode_info(path)
        if not info:
            continue
        inode, meta = info
        task_inodes.add(inode)
        task_paths.append(str(path))
        inode_meta.setdefault(inode, meta)
        known_paths[inode].add(str(path))
    scores = defaultdict(int)
    for inode in task_inodes:
        for key in inode_groups.get(inode, ()):
            scores[key] += 1
    if not scores:
        unmatched_tasks += 1
        seasons = set()
        for path in task_paths:
            match = SEASON_RE.search(path)
            if match:
                seasons.add(int(match.group(1)))
        name_match = SEASON_RE.search(str(row.get("name") or ""))
        if name_match:
            seasons.add(int(name_match.group(1)))
        signature = (
            ("inode", tuple(sorted(task_inodes)))
            if task_inodes
            else (
                "task",
                str(row.get("name") or "").casefold(),
                int(row.get("size") or 0),
            )
        )
        bundle = unmatched_groups.setdefault(
            signature,
            {"inodes": set(), "rows": [], "seasons": set()},
        )
        bundle["inodes"].update(task_inodes)
        bundle["rows"].append(row)
        bundle["seasons"].update(seasons)
        continue
    key = max(scores, key=lambda item: (scores[item], item))
    shared = {inode for inode in task_inodes if key in inode_groups.get(inode, ())}
    seasons = set()
    for inode in shared:
        seasons.update(inode_seasons.get(inode, ()))
    if not seasons:
        for path in task_paths:
            match = SEASON_RE.search(path)
            if match:
                seasons.add(int(match.group(1)))
    group_tasks[key].append(
        make_task(
            row,
            season_label(seasons, groups[key]["seasons"])
            if groups[key]["media_type"] == "tv"
            else "整部",
            hr_hashes,
            hr_candidate_hashes,
            hr_available,
        )
    )

hardlink_paths, hardlink_scan_verified = hardlink_path_index()
for inode in list(inode_meta):
    known_paths[inode].update(hardlink_paths.get(inode, ()))

resources = []
for group in groups.values():
    inodes = {
        inode
        for inode, keys in inode_groups.items()
        if group["key"] in keys
    }
    if not inodes:
        continue
    tasks = group_tasks.get(group["key"], [])
    title = first_nonempty(group["names"], has_cjk) or first_nonempty(group["names"])
    english = (
        first_nonempty(group["original_titles"], lambda value: not has_cjk(value))
        or first_nonempty(group["names"], lambda value: not has_cjk(value))
        or title
    )
    year = min(group["years"]) if group["years"] else ""
    total_bytes = sum(inode_meta[inode]["size"] for inode in inodes)
    all_links_known = hardlink_scan_verified and all(
        len(known_paths[inode]) >= inode_meta[inode]["nlink"] for inode in inodes
    )
    hr = any(task["hr"] for task in tasks)
    brush = any(task["brush"] for task in tasks)
    unfinished = any(task["progress"] < 1 for task in tasks)
    hr_unknown = any(task["hr_unknown"] for task in tasks)
    self_publish = any(task["self_publish"] for task in tasks)
    protected = hr or hr_unknown or unfinished
    if group["media_type"] == "tv":
        seasons = sorted(group["seasons"])
        season_text = season_label(seasons, seasons) if seasons else "季数待识别"
        edition = f"{season_text} · {group['episode_count']} 集"
        library_summary = f"已入库 · {len(seasons)} 季" if seasons else "已入库"
        library_detail = f"Jellyfin 可播放 · {group['episode_count']} 集"
    else:
        edition = "电影"
        library_summary = "已入库"
        library_detail = "Jellyfin 可播放"
    if tasks:
        impact_title = f"同时影响 {len(tasks)} 个 qB / PT 任务"
        impact_detail = "、".join(f"{task['site']} {task['scope']}" for task in tasks[:4])
        if len(tasks) > 4:
            impact_detail += f" 等 {len(tasks)} 项"
    else:
        impact_title = "不会影响当前做种"
        impact_detail = "完整删除会让该资源从媒体库消失"
    resource_id = stable_resource_id(group["key"])
    resources.append(
        {
            "id": resource_id,
            "title": title,
            "englishTitle": english,
            "edition": edition,
            "type": "电视剧" if group["media_type"] == "tv" else "电影",
            "year": str(year),
            "monogram": "",
            "palette": "",
            "size": round(total_bytes / 1024 ** 3, 3),
            "sizeLabel": human_size(total_bytes),
            "reclaimLabel": ("完整删除可释放 " if all_links_known else "最多可释放 ") + human_size(total_bytes),
            "badges": [],
            "library": True,
            "hr": hr,
            "hrPending": hr_unknown,
            "brush": brush,
            "protected": protected,
            "qbSummary": f"{len(tasks)} 个 qB 任务" if tasks else "无 qB 任务",
            "siteSummary": " · ".join(sorted({task["site"] for task in tasks})) if tasks else "媒体库",
            "librarySummary": library_summary,
            "libraryDetail": library_detail,
            "seedTasks": [
                {key: task[key] for key in ("site", "scope", "status", "tone")}
                for task in tasks
            ] or None,
            "impactTitle": impact_title,
            "impactDetail": impact_detail,
            "_sort": total_bytes,
            "_self_publish": self_publish,
            "_private": private_record(
                group["key"],
                inodes,
                tasks,
                library_cleanup_roots(group),
                all_links_known,
                group["moviepilot_indexes"],
                moviepilot_index_source_available,
            ),
        }
    )

for bundle in unmatched_groups.values():
    rows = bundle["rows"]
    inodes = bundle["inodes"]
    seasons = sorted(bundle["seasons"])
    release_name = re.sub(
        r"\s+",
        " ",
        re.sub(r"[._]+", " ", str(rows[0].get("name") or "未命名 qB 任务")),
    ).strip()
    media_type = "tv" if seasons else "movie"
    title = release_name if has_cjk(release_name) else "中文名待识别"
    english = "英文名待识别" if looks_like_infohash(release_name) else release_name
    total_bytes = sum(inode_meta[inode]["size"] for inode in inodes)
    if not total_bytes:
        total_bytes = max(
            int(row.get("completed") or 0)
            or int(float(row.get("size") or 0) * float(row.get("progress") or 0))
            for row in rows
        )
    all_links_known = bool(inodes) and all(
        len(known_paths[inode]) >= inode_meta[inode]["nlink"] for inode in inodes
    )
    tasks = []
    for row in rows:
        tasks.append(
            make_task(
                row,
                season_label(seasons, seasons) if media_type == "tv" else "整部",
                hr_hashes,
                hr_candidate_hashes,
                hr_available,
            )
        )
    hr = any(task["hr"] for task in tasks)
    brush = any(task["brush"] for task in tasks)
    unfinished = any(task["progress"] < 1 for task in tasks)
    hr_unknown = any(task["hr_unknown"] for task in tasks)
    protected = hr or hr_unknown or unfinished
    edition = (
        season_label(seasons, seasons) + " · 未入库"
        if media_type == "tv"
        else "下载区资源 · 未入库"
    )
    impact_title = f"同时影响 {len(tasks)} 个 qB / PT 任务"
    impact_detail = "、".join(f"{task['site']} {task['scope']}" for task in tasks[:4])
    if len(tasks) > 4:
        impact_detail += f" 等 {len(tasks)} 项"
    inode_identity = "|".join(
        f"{inode[0]}:{inode[1]}:{inode_meta[inode]['size']}"
        for inode in sorted(inodes)
    )
    fallback_identity = "|".join(sorted(task["_hash"] for task in tasks))
    private_identity = f"qb:{inode_identity or fallback_identity}"
    resource_id = stable_resource_id(private_identity)
    resources.append(
        {
            "id": resource_id,
            "title": title,
            "englishTitle": english,
            "edition": edition,
            "type": "电视剧" if media_type == "tv" else "电影",
            "year": "",
            "monogram": "",
            "palette": "",
            "size": round(total_bytes / 1024 ** 3, 3),
            "sizeLabel": human_size(total_bytes),
            "reclaimLabel": (
                ("完整删除可释放 " if all_links_known else "最多可释放 ")
                + human_size(total_bytes)
            ),
            "badges": [],
            "library": False,
            "hr": hr,
            "hrPending": hr_unknown,
            "brush": brush,
            "protected": protected,
            "qbSummary": f"{len(tasks)} 个 qB 任务",
            "siteSummary": " · ".join(sorted({task["site"] for task in tasks})),
            "librarySummary": "未入库",
            "libraryDetail": "仅在 qB / 下载区",
            "seedTasks": [
                {key: task[key] for key in ("site", "scope", "status", "tone")}
                for task in tasks
            ],
            "impactTitle": impact_title,
            "impactDetail": impact_detail,
            "_sort": total_bytes,
            "_self_publish": any(task["self_publish"] for task in tasks),
            "_private": private_record(
                private_identity,
                inodes,
                tasks,
                [],
                all_links_known,
                [],
                moviepilot_index_source_available,
            ),
        }
    )

resources.sort(key=lambda item: (-item["_sort"], item["title"]))
for item in resources:
    item.pop("_sort", None)
    item.pop("_self_publish", None)

unresolved_plan_ids = unresolved_transactions()
print(
    json.dumps(
        {
            "schemaVersion": 2,
            "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": "PiNAS read-only snapshot",
            "stats": {
                "resources": len(resources),
                "jellyfinGroups": len(groups),
                "qbTasks": len(torrents),
                "qbFileListsCached": qb_file_lists_cached,
                "matchedQbTasks": sum(len(tasks) for tasks in group_tasks.values()),
                "unmatchedQbTasks": unmatched_tasks,
                "hrSourceAvailable": hr_available,
                "hrActiveTitles": hr_status["activeCount"],
                "hrMatchedQbTasks": len(hr_hashes),
                "hrMissingQbTasks": hr_status["missingCount"],
                "hrMissingUncovered": hr_status[
                    "missingUncoveredCount"
                ],
                "hrRecoveryCandidates": len(hr_candidate_hashes),
                "unresolvedTransactions": len(unresolved_plan_ids),
                "moviepilotIndexSourceAvailable": bool(
                    moviepilot_index_source_available
                ),
            },
            "_hrHashCache": hr_status["hashCache"],
            "_hrMissingRecords": hr_status["missingRecords"],
            "_qbFileCache": next_qb_file_cache,
            "_unresolvedTransactionIds": unresolved_plan_ids,
            "resources": resources,
        },
        ensure_ascii=False,
    )
)
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default="nas-user@192.0.2.1",
        help="SSH target; key-based authentication is required.",
    )
    parser.add_argument(
        "--local-nas",
        action="store_true",
        help="Collect directly on the Pi instead of connecting over SSH.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "public/data/resource-snapshot.json",
    )
    parser.add_argument(
        "--private-output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".runtime/resource-inventory.json",
        help="Private action inventory; never serve this file from the web root.",
    )
    parser.add_argument(
        "--hr-cache",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / ".runtime/hr-infohash-cache.json",
        help="Private cache of immutable BTSchool torrent id to infohash mappings.",
    )
    parser.add_argument(
        "--qb-file-cache",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / ".runtime/qb-file-cache.json",
        help="Private cache of immutable completed-torrent file lists.",
    )
    parser.add_argument(
        "--metadata-cache",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / ".runtime/media-metadata-cache.json",
        help="Private cache of sanitized MoviePilot name recognition results.",
    )
    parser.add_argument(
        "--metadata-overrides",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "db/media-name-overrides.json",
        help="Audited exact-query bilingual name overrides.",
    )
    parser.add_argument(
        "--metadata-resolve-limit",
        type=int,
        default=128,
        help="Maximum uncached qB-only names resolved per refresh.",
    )
    parser.add_argument(
        "--metadata-retry-unresolved",
        action="store_true",
        help="Retry recently unresolved names during an explicit maintenance run.",
    )
    return parser.parse_args()


def write_json_atomic(path: Path, payload: dict, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
        temp_path = Path(handle.name)
    os.chmod(temp_path, mode)
    os.replace(temp_path, path)


def sanitize_qb_file_cache(value: object) -> dict[str, list[dict]]:
    if not isinstance(value, dict) or len(value) > 5000:
        return {}
    result: dict[str, list[dict]] = {}
    total_items = 0
    for raw_hash, raw_items in value.items():
        task_hash = str(raw_hash).lower()
        if (
            len(task_hash) != 40
            or any(character not in "0123456789abcdef" for character in task_hash)
            or not isinstance(raw_items, list)
            or len(raw_items) > 10000
        ):
            continue
        items = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            name = raw_item.get("name")
            size = raw_item.get("size")
            progress = raw_item.get("progress")
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 4096
                or not isinstance(size, int)
                or size < 0
                or not isinstance(progress, (int, float))
                or not 0 <= float(progress) <= 1
            ):
                continue
            items.append(
                {
                    "name": name,
                    "size": size,
                    "progress": float(progress),
                }
            )
        total_items += len(items)
        if total_items > 200000:
            return {}
        if items:
            result[task_hash] = items
    return result


def main() -> int:
    args = parse_args()
    hr_hash_cache = {}
    try:
        with args.hr_cache.open(encoding="utf-8") as handle:
            candidate_cache = json.load(handle)
        if isinstance(candidate_cache, dict):
            hr_hash_cache = {
                str(torrent_id): str(task_hash).lower()
                for torrent_id, task_hash in candidate_cache.items()
                if str(torrent_id).isdigit()
                and len(str(task_hash)) == 40
                and all(
                    character in "0123456789abcdefABCDEF"
                    for character in str(task_hash)
                )
            }
    except (OSError, json.JSONDecodeError):
        pass
    qb_file_cache = {}
    try:
        with args.qb_file_cache.open(encoding="utf-8") as handle:
            qb_file_cache = sanitize_qb_file_cache(json.load(handle))
    except (OSError, json.JSONDecodeError):
        pass
    metadata_cache: dict = {"version": 1, "entries": {}}
    try:
        with args.metadata_cache.open(encoding="utf-8") as handle:
            metadata_cache = sanitize_metadata_cache(json.load(handle))
    except (OSError, json.JSONDecodeError):
        pass
    with args.metadata_overrides.open(encoding="utf-8") as handle:
        metadata_overrides = json.load(handle)
    remote_collector = REMOTE_COLLECTOR.replace(
        "__HR_HASH_CACHE__",
        repr(hr_hash_cache),
        1,
    ).replace(
        "__QB_FILE_CACHE__",
        repr(qb_file_cache),
        1,
    )
    collector_command = (
        ["sudo", "-n", "/usr/bin/python3", "-"]
        if args.local_nas
        else [
            "ssh",
            "-o",
            "BatchMode=yes",
            args.host,
            "sudo",
            "-n",
            "/usr/bin/python3",
            "-",
        ]
    )
    result = subprocess.run(
        collector_command,
        input=remote_collector,
        text=True,
        check=True,
        capture_output=True,
        timeout=300,
    )
    raw_payload = json.loads(result.stdout)
    raw_hr_hash_cache = raw_payload.pop("_hrHashCache", {})
    raw_hr_missing_records = raw_payload.pop("_hrMissingRecords", [])
    raw_qb_file_cache = raw_payload.pop("_qbFileCache", {})
    raw_unresolved_transaction_ids = raw_payload.pop(
        "_unresolvedTransactionIds",
        [],
    )
    next_hr_hash_cache = (
        {
            str(torrent_id): str(task_hash).lower()
            for torrent_id, task_hash in raw_hr_hash_cache.items()
            if str(torrent_id).isdigit()
            and len(str(task_hash)) == 40
            and all(
                character in "0123456789abcdefABCDEF"
                for character in str(task_hash)
            )
        }
        if isinstance(raw_hr_hash_cache, dict)
        else {}
    )
    next_qb_file_cache = sanitize_qb_file_cache(raw_qb_file_cache)
    hr_metadata_resources = make_hr_metadata_resources(
        raw_hr_missing_records
    )
    metadata_cache, metadata_source_available = resolve_media_names(
        host=None if args.local_nas else args.host,
        resources=[
            *hr_metadata_resources,
            *raw_payload["resources"],
        ],
        cache=metadata_cache,
        limit=max(0, min(args.metadata_resolve_limit, 256)),
        retry_unresolved=args.metadata_retry_unresolved,
    )
    metadata_cache, metadata_manual_overrides = apply_metadata_overrides(
        [
            *hr_metadata_resources,
            *raw_payload["resources"],
        ],
        metadata_cache,
        metadata_overrides,
    )
    metadata_cache = prune_metadata_cache(
        [
            *hr_metadata_resources,
            *raw_payload["resources"],
        ],
        metadata_cache,
    )
    enriched_resources, metadata_stats = enrich_and_merge_resources(
        raw_payload["resources"],
        metadata_cache,
    )
    (
        enriched_resources,
        raw_hr_missing_records,
        hr_link_stats,
    ) = annotate_hr_missing_resources(
        enriched_resources,
        raw_hr_missing_records,
        metadata_cache,
    )
    raw_payload["resources"] = enriched_resources
    raw_payload["stats"].update(metadata_stats)
    raw_payload["stats"].update(hr_link_stats)
    raw_payload["stats"]["metadataSourceAvailable"] = (
        metadata_source_available
    )
    raw_payload["stats"]["metadataManualOverrides"] = (
        metadata_manual_overrides
    )
    raw_payload["stats"]["resources"] = len(enriched_resources)
    public_payload = copy.deepcopy(raw_payload)
    private_resources = {}
    for raw_item, public_item in zip(
        raw_payload["resources"],
        public_payload["resources"],
        strict=True,
    ):
        private_data = public_item.pop("_private")
        private_resources[raw_item["id"]] = {
            "id": raw_item["id"],
            "title": raw_item["title"],
            "englishTitle": raw_item["englishTitle"],
            "edition": raw_item["edition"],
            "sizeGiB": raw_item["size"],
            "library": raw_item["library"],
            "hr": raw_item["hr"],
            "brush": raw_item["brush"],
            "protected": raw_item["protected"],
            **private_data,
        }
    state_for_digest = {
        "schemaVersion": raw_payload["schemaVersion"],
        "stats": raw_payload["stats"],
        "hrMissingRecords": raw_hr_missing_records,
        "unresolvedTransactionIds": raw_unresolved_transaction_ids,
        "resources": private_resources,
    }
    snapshot_id = "snap_" + hashlib.sha256(
        json.dumps(
            state_for_digest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]
    public_payload["snapshotId"] = snapshot_id
    private_payload = {
        "schemaVersion": raw_payload["schemaVersion"],
        "snapshotId": snapshot_id,
        "generatedAt": raw_payload["generatedAt"],
        "host": args.host,
        "stats": raw_payload["stats"],
        "hrMissingRecords": raw_hr_missing_records,
        "unresolvedTransactionIds": raw_unresolved_transaction_ids,
        "resources": private_resources,
    }
    validate_snapshot_pair(public_payload, private_payload)
    write_json_atomic(args.output, public_payload, 0o644)
    write_json_atomic(args.private_output, private_payload, 0o600)
    write_json_atomic(args.hr_cache, next_hr_hash_cache, 0o600)
    write_json_atomic(args.qb_file_cache, next_qb_file_cache, 0o600)
    write_json_atomic(args.metadata_cache, metadata_cache, 0o600)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "privateOutput": str(args.private_output),
                "snapshotId": snapshot_id,
                "generatedAt": public_payload["generatedAt"],
                "stats": public_payload["stats"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
