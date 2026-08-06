"""Read-only discovery of common NAS cleanup layouts.

Discovery only proposes a configuration.  It never creates directories or
changes qBittorrent, MoviePilot, media files, or services.
"""

from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import sqlite3
import stat
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit
from typing import Any


_DISCOVERY_ROOTS = (
    Path("/config"),
    Path("/data"),
    Path("/home"),
    Path("/mnt"),
    Path("/opt"),
    Path("/share"),
    Path("/srv"),
    Path("/var/lib"),
    Path("/volume1"),
    Path("/volume2"),
)
_DISCOVERY_STORAGE_BASES = (
    Path("/mnt"),
    Path("/volume1"),
    Path("/volume2"),
    Path("/data"),
    Path("/share"),
)
_DISCOVERY_SKIP_DIRS = {
    ".cache",
    ".git",
    ".local/share/trash",
    "completed",
    "downloads",
    "incoming",
    "media",
    "movies",
    "node_modules",
    "proc",
    "sys",
    "staging",
    "tmp",
    "tv",
}
_DIRECT_DISCOVERY_CANDIDATES = {
    "jellyfin_db": (
        "/var/lib/jellyfin/data/jellyfin.db",
        "/config/data/jellyfin.db",
        "/jellyfin/config/data/jellyfin.db",
        "/jellyfin/data/jellyfin.db",
        "/mnt/sdc/library-tools/jellyfin/config/data/jellyfin.db",
        "/mnt/sdd/library-tools/jellyfin/config/data/jellyfin.db",
    ),
    "moviepilot_db": (
        "/config/user.db",
        "/moviepilot/config/user.db",
        "/app/config/user.db",
        "/mnt/sdc/library-tools/moviepilot/config/user.db",
        "/mnt/sdd/library-tools/moviepilot/config/user.db",
    ),
    "qb_backup": (
        "/root/.local/share/qBittorrent/BT_backup",
        "/root/.config/qBittorrent/BT_backup",
        "/config/qBittorrent/BT_backup",
        "/mnt/sdc/library-tools/qBittorrent/BT_backup",
        "/mnt/sdd/library-tools/qBittorrent/BT_backup",
    ),
}


def _safe_file(path: Path) -> bool:
    try:
        result = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(result.st_mode) and not stat.S_ISLNK(result.st_mode)


def _safe_dir(path: Path) -> bool:
    try:
        result = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(result.st_mode) and not stat.S_ISLNK(result.st_mode)


def _bounded_find(names: set[str], *, max_depth: int = 6) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for base in _DISCOVERY_ROOTS:
        if not _safe_dir(base):
            continue
        base_depth = len(base.parts)
        for current, directories, files in os.walk(base, topdown=True):
            current_path = Path(current)
            depth = len(current_path.parts) - base_depth
            directories[:] = [
                name
                for name in directories
                if name.casefold() not in _DISCOVERY_SKIP_DIRS
                and not (current_path / name).is_symlink()
            ]
            if depth >= max_depth:
                directories[:] = []
            for name in (*files, *directories):
                if name not in names:
                    continue
                candidate = current_path / name
                if str(candidate) not in seen:
                    seen.add(str(candidate))
                    found.append(candidate)
    return found


def _path_score(path: Path, keywords: tuple[str, ...]) -> tuple[int, int, str]:
    text = str(path).casefold()
    score = sum(8 for keyword in keywords if keyword.casefold() in text)
    return (-score, len(path.parts), text)


def _select_unique(candidates: list[Path], keywords: tuple[str, ...]) -> tuple[Path | None, list[Path]]:
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    if not unique:
        return None, []
    ranked = sorted(unique, key=lambda item: _path_score(item, keywords))
    best_score = _path_score(ranked[0], keywords)[0]
    best = [item for item in ranked if _path_score(item, keywords)[0] == best_score]
    return (None, best) if len(best) > 1 else (ranked[0], [])


def _sqlite_has_table(path: Path, table: str) -> bool:
    if not _safe_file(path):
        return False
    connection = None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        return connection.execute(
            "select 1 from sqlite_master where type='table' and name=? limit 1",
            (table,),
        ).fetchone() is not None
    except (OSError, sqlite3.Error):
        return False
    finally:
        if connection is not None:
            connection.close()


def _find_file(field: str, table: str) -> tuple[Path | None, list[Path]]:
    name = "jellyfin.db" if field == "jellyfin_db" else "user.db"
    direct = [Path(value) for value in _DIRECT_DISCOVERY_CANDIDATES.get(field, ())]
    candidates = [
        candidate
        for candidate in (*direct, *_bounded_find({name}))
        if _safe_file(candidate) and _sqlite_has_table(candidate, table)
    ]
    keywords = ("jellyfin", "data") if field == "jellyfin_db" else ("moviepilot", "config")
    return _select_unique(candidates, keywords)


def _find_qb_backup() -> tuple[Path | None, list[Path]]:
    direct = [
        Path(value)
        for value in _DIRECT_DISCOVERY_CANDIDATES.get("qb_backup", ())
    ]
    candidates = [
        candidate
        for candidate in (*direct, *_bounded_find({"BT_backup"}))
        if _safe_dir(candidate)
    ]
    return _select_unique(candidates, ("qbittorrent", "bt_backup"))


def _probe_qb_url(url: str) -> bool:
    try:
        request = Request(
            f"{url.rstrip('/')}/api/v2/app/version",
            headers={"Accept": "text/plain", "User-Agent": "PiNAS-Cleanup-Discovery/1"},
        )
        with urlopen(request, timeout=0.8) as response:
            response.read(128)
        return True
    except HTTPError as exc:
        return exc.code in {401, 403}
    except (URLError, OSError):
        return False


def _discover_qb_url(current: object) -> tuple[str, bool]:
    candidates = [
        str(current or "").strip(),
        os.environ.get("QBITTORRENT_URL", "").strip(),
        "http://127.0.0.1:8080",
        "http://localhost:8080",
        "http://qbittorrent:8080",
        "http://qb:8080",
    ]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            normalized = candidate.rstrip("/")
            if urlsplit(normalized).scheme not in {"http", "https"}:
                continue
        except ValueError:
            continue
        if _probe_qb_url(normalized):
            return normalized, True
    fallback = str(current or "").strip() or "http://127.0.0.1:8080"
    return fallback.rstrip("/"), False


def _mountpoint(path: Path) -> Path | None:
    try:
        mounts = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        mounts = []
    resolved = path.resolve()
    candidates: list[Path] = []
    for line in mounts:
        preamble = line.split(" - ", 1)[0].split()
        if len(preamble) < 5:
            continue
        mount = Path(preamble[4].replace("\\040", " ").replace("\\011", "\t"))
        if resolved == mount or mount in resolved.parents:
            candidates.append(mount.resolve())
    if candidates:
        return max(candidates, key=lambda item: len(item.parts))
    for candidate in (resolved, *resolved.parents):
        if os.path.ismount(candidate):
            return candidate.resolve()
    return None


def _media_root(path: Path) -> Path | None:
    if not path.is_absolute():
        return None
    parts = path.parts
    lowered = [part.casefold() for part in parts]
    for marker in (".media-main", "media", "library", "downloads", "download", "completed"):
        if marker not in lowered:
            continue
        index = lowered.index(marker)
        end = index + 1
        if marker in {"downloads", "download"} and end < len(parts):
            if lowered[end] in {"completed", "complete", "finished"}:
                end += 1
        elif marker in {".media-main", "media", "library"} and end < len(parts):
            end += 1
        root = Path(*parts[:end])
        if _safe_dir(root) and root != Path(root.anchor):
            return root
    return None


def _moviepilot_library_roots(moviepilot_db: Path | None) -> list[Path]:
    if moviepilot_db is None or not _safe_file(moviepilot_db):
        return []
    connection = None
    try:
        connection = sqlite3.connect(f"file:{moviepilot_db}?mode=ro", uri=True, timeout=3)
        rows = connection.execute(
            "select path from mediaserveritem where path is not null limit 5000"
        ).fetchall()
    except (OSError, sqlite3.Error):
        return []
    finally:
        if connection is not None:
            connection.close()
    result: list[Path] = []
    seen: set[str] = set()
    for row in rows:
        root = _media_root(Path(str(row[0] or "")))
        if root is not None and str(root) not in seen:
            seen.add(str(root))
            result.append(root)
    return result


def _casefold_subdir(parent: Path, parts: tuple[str, ...]) -> Path | None:
    current = parent
    for part in parts:
        try:
            matches = [child for child in current.iterdir() if child.name.casefold() == part.casefold() and _safe_dir(child)]
        except OSError:
            return None
        if len(matches) != 1:
            return None
        current = matches[0]
    return current


def _common_library_roots() -> list[Path]:
    roots: list[Path] = []
    volumes: list[Path] = []
    for base in _DISCOVERY_STORAGE_BASES:
        if not _safe_dir(base):
            continue
        volumes.append(base)
        try:
            volumes.extend(child for child in base.iterdir() if not child.name.startswith(".") and _safe_dir(child))
        except OSError:
            continue
    layouts = (
        ("downloads", "completed"),
        ("downloads", "complete"),
        ("downloads", "finished"),
        ("media", "movies"),
        ("media", "tv"),
        ("library",),
        (".media-main", "movies"),
        (".media-main", "tv"),
        ("movies",),
        ("tv",),
    )
    for volume in volumes:
        for layout in layouts:
            candidate = _casefold_subdir(volume, layout)
            if candidate is not None and candidate not in roots:
                roots.append(candidate)
    return roots


def _valid_allowed_roots(values: list[Path]) -> list[str]:
    result: list[str] = []
    for root in values:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        volume = _mountpoint(resolved)
        if not _safe_dir(resolved) or volume is None or resolved == volume:
            continue
        if str(resolved) not in result:
            result.append(str(resolved))
    return result


def discover_config(current: dict[str, Any] | None = None, *, project_root: Path | None = None) -> dict[str, Any]:
    """Return a read-only configuration proposal and diagnostic checks."""

    # Import lazily to keep configuration.py's public API backwards-compatible
    # without introducing a module-import cycle.
    try:
        from .configuration import default_config, normalize_config, probe_config
    except (ImportError, ModuleNotFoundError):
        from scripts.configuration import default_config, normalize_config, probe_config

    base = default_config()
    if isinstance(current, dict):
        base.update({key: deepcopy(value) for key, value in current.items() if key in base})
    project_root = (project_root or Path(__file__).resolve().parents[1]).resolve()
    ambiguities: dict[str, list[str]] = {}

    moviepilot_db = Path(str(base.get("moviepilot_db") or ""))
    if not _sqlite_has_table(moviepilot_db, "mediaserveritem"):
        moviepilot_db, candidates = _find_file("moviepilot_db", "mediaserveritem")
        if candidates:
            ambiguities["moviepilot_db"] = [str(item.resolve()) for item in candidates]
    if moviepilot_db is not None and _sqlite_has_table(moviepilot_db, "mediaserveritem"):
        base["moviepilot_db"] = str(moviepilot_db.resolve())

    jellyfin_db = Path(str(base.get("jellyfin_db") or ""))
    if not _sqlite_has_table(jellyfin_db, "BaseItems"):
        jellyfin_db, candidates = _find_file("jellyfin_db", "BaseItems")
        if candidates:
            ambiguities["jellyfin_db"] = [str(item.resolve()) for item in candidates]
    if jellyfin_db is not None and _sqlite_has_table(jellyfin_db, "BaseItems"):
        base["jellyfin_db"] = str(jellyfin_db.resolve())

    qb_backup = Path(str(base.get("qb_backup") or ""))
    if not _safe_dir(qb_backup):
        qb_backup, candidates = _find_qb_backup()
        if candidates:
            ambiguities["qb_backup"] = [str(item.resolve()) for item in candidates]
    if qb_backup is not None and _safe_dir(qb_backup):
        base["qb_backup"] = str(qb_backup.resolve())

    base["qb_url"], qb_url_reachable = _discover_qb_url(base.get("qb_url"))
    current_roots = [
        Path(str(value))
        for value in base.get("allowed_roots", [])
        if isinstance(value, str) and not value.startswith("/path/to/")
    ]
    roots = _valid_allowed_roots(current_roots)
    if not roots:
        roots = _valid_allowed_roots(_moviepilot_library_roots(moviepilot_db) + _common_library_roots())
    if roots:
        base["allowed_roots"] = roots

    quarantine: dict[str, str] = {}
    for volume, target in (base.get("quarantine_roots") or {}).items():
        volume_path = Path(str(volume))
        mounted = _mountpoint(volume_path)
        if mounted is not None:
            mounted = mounted.resolve()
        if volume_path != Path("/") and _safe_dir(volume_path) and mounted == volume_path.resolve():
            quarantine[str(volume_path)] = str(Path(str(target)))
    if not quarantine:
        for root_value in base.get("allowed_roots", []):
            volume = _mountpoint(Path(str(root_value)))
            if volume is not None:
                volume = volume.resolve()
            if volume is not None and volume not in {Path("/"), Path(str(root_value))}:
                quarantine[str(volume)] = str(volume / ".storage-cleanup-quarantine")
    if quarantine:
        base["quarantine_roots"] = quarantine

    execution_backup = Path(str(base.get("execution_backup") or ""))
    if not _safe_dir(execution_backup):
        base["execution_backup"] = str(project_root / "shared" / "qb-backups")

    normalized = normalize_config(base)
    probe = probe_config(normalized)
    checks: list[dict[str, Any]] = []
    for key, label in (
        ("qb_url", "qBittorrent 服务"),
        ("moviepilot_db", "MoviePilot 数据库"),
        ("jellyfin_db", "Jellyfin 数据库"),
        ("qb_backup", "qB 种子备份"),
        ("allowed_roots", "媒体与下载目录"),
        ("quarantine_roots", "同盘安全隔离区"),
        ("execution_backup", "清理事务备份"),
    ):
        value = normalized.get(key)
        if key == "qb_url":
            found = qb_url_reachable
        elif key == "allowed_roots":
            found = bool(value) and not all(str(item).startswith("/path/to/") for item in value)
        elif key == "quarantine_roots":
            found = bool(value) and not any(str(item).startswith("/path/to/") for item in value.values())
        else:
            found = isinstance(value, str) and not value.startswith("/path/to/") and (
                _safe_file(Path(value)) if key.endswith("_db") else _safe_dir(Path(value))
            )
        will_create = key == "execution_backup" and not found and Path(str(value)).name == "qb-backups" and project_root in Path(str(value)).resolve(strict=False).parents
        if key == "quarantine_roots" and not found:
            will_create = all(
                Path(str(target)).name == ".storage-cleanup-quarantine"
                and Path(str(target)).parent == Path(str(volume))
                and _safe_dir(Path(str(volume)))
                for volume, target in (value or {}).items()
            )
        checks.append({
            "key": key,
            "label": label,
            "found": found,
            "willCreate": will_create,
            "ambiguous": key in ambiguities,
            "candidates": ambiguities.get(key, []),
        })
    return {
        "config": normalized,
        "probe": probe,
        "checks": checks,
        "ambiguities": ambiguities,
        "ready": all(item["found"] or item["willCreate"] for item in checks) and probe["ok"],
        "readOnly": True,
    }
