"""Configuration, validation, and read-only probing for NAS cleanup.

The cleanup engine deliberately keeps its safety policy in code, but the
machine-specific topology belongs in a host-side configuration file.  This
module contains no credentials and is safe to use from the MoviePilot
settings endpoint.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit
from typing import Any


CONFIG_VERSION = 1


DEFAULT_CONFIG: dict[str, Any] = {
    "version": CONFIG_VERSION,
    "ssh_host": "nas-user@nas.example.lan",
    "qb_url": "http://127.0.0.1:8080",
    "jellyfin_db": "/var/lib/jellyfin/data/jellyfin.db",
    "moviepilot_db": "/path/to/moviepilot/config/user.db",
    "qb_backup": "/path/to/qBittorrent/BT_backup",
    "execution_backup": "/path/to/storage-cleanup/qb-backups",
    # Optional, host-local evidence of torrents that were published by this
    # account.  An empty list keeps generic installs portable; the collector
    # still checks its PiNAS default path when this is unset.
    "publication_ledger_roots": [],
    "allowed_roots": [
        "/path/to/downloads/completed",
        "/path/to/media/movies",
        "/path/to/media/tv",
    ],
    "quarantine_roots": {
        "/path/to": "/path/to/.storage-cleanup-quarantine",
    },
}

PATH_FIELDS = (
    "jellyfin_db",
    "moviepilot_db",
    "qb_backup",
    "execution_backup",
)


class ConfigurationError(ValueError):
    """Raised when a configuration cannot be safely interpreted."""


def default_config() -> dict[str, Any]:
    return deepcopy(DEFAULT_CONFIG)


def _path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{field} 必须是非空字符串。")
    candidate = value.strip()
    if "\x00" in candidate or not candidate.startswith("/"):
        raise ConfigurationError(f"{field} 必须是绝对路径。")
    parsed = Path(candidate)
    if any(part == ".." for part in parsed.parts):
        raise ConfigurationError(f"{field} 不能包含 .. 路径段。")
    if parsed == Path("/"):
        raise ConfigurationError(f"{field} 不能指向系统根目录。")
    return str(parsed)


def normalize_config(raw: object) -> dict[str, Any]:
    if raw is None:
        return default_config()
    if not isinstance(raw, dict):
        raise ConfigurationError("配置必须是 JSON 对象。")
    merged = default_config()
    merged.update({key: value for key, value in raw.items() if key in merged})
    try:
        version = int(merged.get("version", CONFIG_VERSION))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("version 必须是数字。") from exc
    if version != CONFIG_VERSION:
        raise ConfigurationError(f"不支持的配置版本：{version}。")
    merged["version"] = CONFIG_VERSION
    for field in PATH_FIELDS:
        merged[field] = _path(merged[field], field)
    raw_publication_roots = merged.get("publication_ledger_roots")
    if raw_publication_roots is None:
        raw_publication_roots = []
    if not isinstance(raw_publication_roots, list):
        raise ConfigurationError("publication_ledger_roots 必须是路径数组。")
    publication_roots: list[str] = []
    for index, value in enumerate(raw_publication_roots):
        item = _path(value, f"publication_ledger_roots[{index}]")
        if item not in publication_roots:
            publication_roots.append(item)
    merged["publication_ledger_roots"] = publication_roots
    if not isinstance(merged.get("allowed_roots"), list):
        raise ConfigurationError("allowed_roots 必须是路径数组。")
    roots: list[str] = []
    for index, value in enumerate(merged["allowed_roots"]):
        item = _path(value, f"allowed_roots[{index}]")
        if item not in roots:
            roots.append(item)
    if not roots:
        raise ConfigurationError("至少需要一个 allowed_roots。")
    merged["allowed_roots"] = roots
    raw_quarantine = merged.get("quarantine_roots")
    if not isinstance(raw_quarantine, dict) or not raw_quarantine:
        raise ConfigurationError("quarantine_roots 必须是非空对象。")
    quarantine: dict[str, str] = {}
    for volume, target in raw_quarantine.items():
        volume_path = _path(volume, "quarantine_roots volume")
        quarantine[volume_path] = _path(target, f"quarantine_roots[{volume}]")
    merged["quarantine_roots"] = quarantine
    volume_paths = tuple(Path(value) for value in quarantine)
    quarantine_paths = tuple(Path(value) for value in quarantine.values())
    for root in (Path(value) for value in roots):
        if any(root == volume for volume in volume_paths):
            raise ConfigurationError(
                f"allowed_roots 不能直接开放整个数据卷：{root}。"
            )
        if not any(volume in root.parents for volume in volume_paths):
            raise ConfigurationError(
                f"allowed_roots 必须位于已配置卷根目录下：{root}。"
            )
        if any(
            root == quarantine_path or quarantine_path in root.parents
            for quarantine_path in quarantine_paths
        ):
            raise ConfigurationError(
                f"allowed_roots 不能覆盖清理隔离目录：{root}。"
            )
    for field in ("qb_backup", "execution_backup"):
        backup_path = Path(merged[field])
        if any(
            root == backup_path or root in backup_path.parents
            for root in (Path(value) for value in roots)
        ):
            raise ConfigurationError(f"{field} 不能位于允许清理根目录内。")
    qb_url = merged.get("qb_url")
    if not isinstance(qb_url, str) or urlsplit(qb_url).scheme not in {"http", "https"}:
        raise ConfigurationError("qb_url 必须是 http 或 https 地址。")
    merged["qb_url"] = qb_url.rstrip("/")
    ssh_host = merged.get("ssh_host")
    if not isinstance(ssh_host, str) or not ssh_host.strip() or any(
        character.isspace() for character in ssh_host
    ):
        raise ConfigurationError("ssh_host 必须是无空格的 SSH 目标。")
    merged["ssh_host"] = ssh_host.strip()
    return merged


def load_config(path: Path | None = None) -> dict[str, Any]:
    if path is None:
        configured_path = os.environ.get("PINAS_CLEANUP_CONFIG")
        path = (
            Path(configured_path)
            if configured_path
            else Path(__file__).resolve().parents[1] / ".runtime/config.json"
        )
    try:
        with path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return default_config()
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"无法读取配置：{path}") from exc
    return normalize_config(raw)


def write_config(path: Path, raw: object) -> dict[str, Any]:
    config = normalize_config(raw)
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    try:
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return config


def config_fingerprint(config: dict[str, Any]) -> str:
    encoded = json.dumps(
        normalize_config(config), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def probe_config(config: dict[str, Any]) -> dict[str, Any]:
    """Inspect configured paths without creating, moving, or deleting files."""

    normalized = normalize_config(config)
    entries: list[dict[str, Any]] = []
    problems: list[str] = []
    missing: list[str] = []

    def inspect(value: str, kind: str, *, missing_allowed: bool = False) -> None:
        path = Path(value)
        try:
            stat_result = path.lstat()
            is_symlink = path.is_symlink()
            entries.append(
                {
                    "path": value,
                    "kind": kind,
                    "exists": True,
                    "isDirectory": path.is_dir(),
                    "isFile": path.is_file(),
                    "isSymlink": is_symlink,
                    "device": int(stat_result.st_dev),
                }
            )
            if is_symlink:
                problems.append(f"{value} 是符号链接，不能作为安全边界。")
        except FileNotFoundError:
            entries.append(
                {
                    "path": value,
                    "kind": kind,
                    "exists": False,
                    "missingAllowed": missing_allowed,
                }
            )
            if not missing_allowed:
                missing.append(value)
        except OSError as exc:
            entries.append({"path": value, "kind": kind, "exists": False, "error": str(exc)})
            problems.append(f"无法读取 {value}：{exc}")

    for field in PATH_FIELDS:
        inspect(normalized[field], field)
    for value in normalized["allowed_roots"]:
        inspect(value, "allowed_root")
    for volume, value in normalized["quarantine_roots"].items():
        inspect(volume, "quarantine_volume")
        # Execution creates the configured quarantine root just before it
        # stages the first file and removes the empty root after commit or
        # rollback. Its absence is therefore a normal idle state, provided
        # the root is the direct child of an existing configured volume.
        inspect(value, "quarantine_root", missing_allowed=True)

    for root in normalized["allowed_roots"]:
        path = Path(root)
        if path.exists() and not path.is_dir():
            problems.append(f"允许根目录不是目录：{root}")
    for field in ("jellyfin_db", "moviepilot_db"):
        path = Path(normalized[field])
        if path.exists() and not path.is_file():
            problems.append(f"{field} 必须是数据库文件：{path}")
    for field in ("qb_backup", "execution_backup"):
        path = Path(normalized[field])
        if path.exists() and not path.is_dir():
            problems.append(f"{field} 必须是目录：{path}")
    for volume, quarantine in normalized["quarantine_roots"].items():
        volume_path = Path(volume)
        quarantine_path = Path(quarantine)
        if volume_path.exists() and not volume_path.is_dir():
            problems.append(f"卷根目录不是目录：{volume}")
        if quarantine_path.exists() and not quarantine_path.is_dir():
            problems.append(f"隔离目录不是目录：{quarantine}")
        if volume_path.exists() and quarantine_path.exists():
            try:
                if volume_path.stat().st_dev != quarantine_path.stat().st_dev:
                    problems.append(f"隔离目录必须与卷位于同一文件系统：{quarantine}")
            except OSError as exc:
                problems.append(f"无法核对文件系统：{exc}")
        if not quarantine_path.exists() and volume_path.exists():
            if quarantine_path.parent != volume_path:
                problems.append(
                    "缺失的隔离目录必须是已配置卷根目录的直接子目录："
                    f"{quarantine}"
                )

    return {
        "ok": not problems and not missing,
        "structurallyValid": True,
        "configFingerprint": config_fingerprint(normalized),
        "missing": missing,
        "problems": problems,
        "entries": entries,
    }
