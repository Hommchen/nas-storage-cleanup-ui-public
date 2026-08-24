#!/opt/homebrew/bin/python3.12
"""Fail-closed integrity checks for public/private cleanup snapshots."""

from __future__ import annotations

import json
import re
from typing import Any


FORBIDDEN_PUBLIC_KEYS = {
    "_private",
    "hash",
    "tracker",
    "contentpath",
    "savepath",
    "exactfiles",
    "cleanupfiles",
    "files",
    "roots",
    "identity",
    "mediapath",
    "downloadpath",
    "linkcount",
    "removelibrary",
    "stopseeding",
    "deleteall",
}
HASH_RE = re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")


def _has_cjk(value: object) -> bool:
    return any("\u3400" <= char <= "\u9fff" for char in str(value or ""))


def _has_latin(value: object) -> bool:
    return bool(re.search(r"[A-Za-z]", str(value or "")))


def _has_verified_public_name(item: dict[str, Any]) -> bool:
    """Mirror the metadata collector's safe public-name rule."""

    title = str(item.get("title") or "").strip()
    english = str(item.get("englishTitle") or "").strip()
    if "待识别" in title or "待核" in title:
        return False
    if not _has_cjk(title):
        return bool(
            item.get("metadataProviderVerified") is True
            and _has_latin(title)
            and _has_latin(english)
        )
    if not item.get("library") and (
        title == english or not _has_latin(english)
    ):
        return False
    return True


def _public_value_is_sanitized(value: object) -> bool:
    if isinstance(value, dict):
        return all(
            str(key).casefold() not in FORBIDDEN_PUBLIC_KEYS
            and _public_value_is_sanitized(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return all(_public_value_is_sanitized(child) for child in value)
    if isinstance(value, str):
        folded = value.casefold()
        return (
            "/mnt/" not in value
            and "passkey=" not in folded
            and "announce=" not in folded
            and not HASH_RE.search(value)
        )
    return True


def _count(stats: dict[str, Any], key: str) -> int:
    value = stats.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"snapshot statistic {key} is invalid")
    return value


def validate_snapshot_pair(
    public: object,
    private: object,
) -> None:
    """Raise ValueError unless both documents form one safe schema-v2 pair."""

    if not isinstance(public, dict) or not isinstance(private, dict):
        raise ValueError("snapshot documents must be objects")
    if (
        public.get("schemaVersion") != 2
        or private.get("schemaVersion") != 2
    ):
        raise ValueError("unsupported snapshot schema")
    snapshot_id = public.get("snapshotId")
    if (
        not isinstance(snapshot_id, str)
        or not re.fullmatch(r"snap_[0-9a-f]{24}", snapshot_id)
        or private.get("snapshotId") != snapshot_id
    ):
        raise ValueError("snapshot ids do not match")
    if (
        not isinstance(public.get("generatedAt"), str)
        or private.get("generatedAt") != public.get("generatedAt")
    ):
        raise ValueError("snapshot timestamps do not match")
    public_stats = public.get("stats")
    private_stats = private.get("stats")
    if (
        not isinstance(public_stats, dict)
        or private_stats != public_stats
    ):
        raise ValueError("snapshot statistics do not match")
    public_resources = public.get("resources")
    private_resources = private.get("resources")
    if (
        not isinstance(public_resources, list)
        or not isinstance(private_resources, dict)
    ):
        raise ValueError("snapshot resources have invalid shape")
    public_ids = [
        item.get("id")
        for item in public_resources
        if isinstance(item, dict)
    ]
    if (
        len(public_ids) != len(public_resources)
        or any(not isinstance(item, str) or not item for item in public_ids)
        or len(public_ids) != len(set(public_ids))
        or set(public_ids) != set(private_resources)
    ):
        raise ValueError("public/private resource ids do not match")
    if _count(public_stats, "resources") != len(public_resources):
        raise ValueError("resource count cannot be reproduced")

    unverified = 0
    for item in public_resources:
        metadata_verified = item.get("metadataVerified")
        if metadata_verified is not True:
            unverified += 1
            if item.get("protected") is not True:
                raise ValueError("unverified metadata is not locked")
        elif not _has_verified_public_name(item):
            raise ValueError("verified metadata lacks a clear public name")
        if item.get("seedTasks") is not None and not isinstance(
            item.get("seedTasks"),
            list,
        ):
            raise ValueError("seed task summary must be a list")
        private_item = private_resources[item["id"]]
        if (
            not isinstance(private_item, dict)
            or private_item.get("id") != item["id"]
            or private_item.get("metadataVerified")
            is not metadata_verified
        ):
            raise ValueError("private resource identity does not match")
        if item.get("metadataProviderVerified") is True and (
            private_item.get("metadataProviderVerified") is not True
        ):
            raise ValueError("provider verification does not match")
    if _count(
        public_stats,
        "metadataUnverifiedResources",
    ) != unverified:
        raise ValueError("unverified metadata count cannot be reproduced")

    hr_active = _count(public_stats, "hrActiveTitles")
    hr_matched = _count(public_stats, "hrMatchedQbTasks")
    hr_missing = _count(public_stats, "hrMissingQbTasks")
    if hr_active != hr_matched + hr_missing:
        raise ValueError("H&R totals do not close")
    if not _public_value_is_sanitized(public):
        raise ValueError("public snapshot contains private execution data")

    # This catches non-string keys and non-JSON values before atomic writes.
    json.dumps(public, ensure_ascii=False, allow_nan=False)
    json.dumps(private, ensure_ascii=False, allow_nan=False)
