#!/opt/homebrew/bin/python3.12
"""Resolve and merge qB-only media rows without exposing MoviePilot credentials."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import base64
import copy
import hashlib
import json
import re
import subprocess
from typing import Any


PUBLIC_HASH_RE = re.compile(
    r"(?i)(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])"
)


CACHE_VERSION = 1
RESULT_SENTINEL = "__PINAS_MEDIA_NAMES__"
RETRY_AFTER = timedelta(hours=6)
YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
MANUAL_IDENTITY_RE = re.compile(
    r"^(movie|tv):manual:[a-z0-9][a-z0-9-]{1,79}$"
)
SEASON_RANGE_RE = re.compile(
    r"(?i)S(\d{1,2})(?:\s*[-–—]\s*S?(\d{1,2}))?"
)
EPISODE_VALUE_RE = re.compile(r"(?i)^S(\d{1,2})E(\d{1,3})$")
DOWNLOAD_STATES = frozenset(
    {"downloading", "forceddl", "stalleddl", "metadl", "checkingdl"}
)
RELEASE_QUALITY_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:2160p|1080p|720p|576p|480p|4k|8k|uhd|fhd|hd|"
    r"web[- ]?dl|webrip|web|bluray|blu[- ]?ray|bdrip|bdremux|remux|hdtv|"
    r"hdr10(?:plus)?|hdr|dv|dolby[ -]?vision|hevc|x26[45]|h26[45]|avc|"
    r"aac(?:[0-9.]+)?|ac3|ddp(?:[0-9.]+)?|dts(?:[- ]?hd)?|truehd|atmos|"
    r"remastered|proper|repack|complete)(?![A-Za-z0-9])"
)
RELEASE_BRACKET_RE = re.compile(r"^\s*[\[【（(]([^\]】）)]+)[\]】）)]\s*")
RELEASE_SEASON_MARK_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])S\d{1,2}(?:E\d{1,3})?(?![A-Za-z0-9])"
)
RELEASE_CJK_TAGS = frozenset(
    {
        "国语",
        "粤语",
        "中字",
        "简中",
        "繁中",
        "双语",
        "配音",
        "原声",
        "合集",
        "修复",
        "收藏",
    }
)


def has_cjk(value: object) -> bool:
    return any("\u3400" <= char <= "\u9fff" for char in str(value or ""))


def has_latin(value: object) -> bool:
    return bool(re.search(r"[A-Za-z]", str(value or "")))


def _clean_text(value: object, *, maximum: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:maximum]


def _media_kind(item: dict[str, Any]) -> str:
    return "tv" if item.get("type") == "电视剧" else "movie"


def _identity_hint(item: dict[str, Any]) -> str:
    identity = str((item.get("_private") or {}).get("identity") or "")
    return identity if re.fullmatch(r"(?:movie|tv):tmdb:\d+", identity) else ""


def _release_normalized(value: object) -> str:
    return re.sub(
        r"\s+",
        " ",
        re.sub(r"[^0-9A-Za-z\u3400-\u9fff]+", " ", str(value or "")),
    ).strip().casefold()


def _release_prefix(query: str) -> tuple[str, str, str]:
    """Return the title-bearing prefix, year, and first season marker."""

    text = re.sub(r"(?i)\.(?:mkv|mp4|mka|iso|avi)$", "", query).strip()
    text = re.sub(r"[._]+", " ", text)
    year_matches = list(YEAR_RE.finditer(text))
    # A number in the title is common in Asian releases (for example
    # ``Hijack 1971 2024``).  When two years are present, the final year
    # before the quality marker is the release/season year and the earlier
    # one remains part of the title.  Using the first match turned that
    # release into a false ``Hijack (1971)`` candidate.
    year_match = year_matches[-1] if year_matches else None
    if len(year_matches) >= 2:
        separator = text[year_matches[0].end() : year_matches[1].start()]
        if re.fullmatch(r"\s*[-–—]\s*", separator):
            # ``2017-2020`` is a series/runtime range, not a title year
            # followed by a release year.
            year_match = year_matches[0]
    season_match = RELEASE_SEASON_MARK_RE.search(text)
    quality_match = RELEASE_QUALITY_RE.search(text)
    stops = [
        match.start()
        for match in (year_match, season_match, quality_match)
        if match
    ]
    prefix = text[: min(stops)].strip() if stops else text
    year = year_match.group(1) if year_match else ""
    season = season_match.group(0).upper() if season_match else ""
    return prefix, year, season


def _parse_release_label(query: object) -> tuple[str, str, str, str]:
    """Extract explicit bilingual title parts without guessing a provider ID.

    This parser only trusts a CJK label that is visibly paired with a Latin
    title in the same release name. It is deliberately conservative: a
    result can create a name-based identity, but never a TMDB identity.
    """

    raw = _clean_text(query, maximum=2000)
    prefix, year, season = _release_prefix(raw)
    cjk_title = ""
    bracket = RELEASE_BRACKET_RE.match(prefix)
    if bracket:
        candidate = _clean_text(bracket.group(1), maximum=120)
        if has_cjk(candidate) and candidate not in RELEASE_CJK_TAGS:
            cjk_title = candidate
        prefix = prefix[bracket.end() :].strip()
    else:
        prefix = re.sub(
            r"^\s*\[(?:国语|粤语|中字|简中|繁中|双语|配音|原声)\]\s*",
            "",
            prefix,
            flags=re.IGNORECASE,
        )
    if not cjk_title:
        leading = re.match(
            r"^\s*([\u3400-\u9fff][\u3400-\u9fff：:·、，,\-— ]{1,80})"
            r"\s+(?=[A-Za-z])",
            prefix,
        )
        if leading:
            cjk_title = _clean_text(leading.group(1), maximum=120)
            prefix = prefix[leading.end() :].strip()
    if not cjk_title and has_cjk(prefix):
        # Chinese-only qB names are still useful resolver input.  The old
        # parser required a Latin companion and reduced ``隐形人 2020`` to
        # only ``2020``, guaranteeing a failed lookup despite provider data
        # being available for the Chinese title.
        leading_cjk = re.match(
            r"^\s*([\u3400-\u9fff][\u3400-\u9fff：:·、，,\-— ]{0,80})\s*$",
            prefix,
        )
        if leading_cjk:
            cjk_title = _clean_text(leading_cjk.group(1), maximum=120)
    if not cjk_title and season:
        season_position = RELEASE_SEASON_MARK_RE.search(raw)
        if season_position:
            before_season = re.sub(r"[._]+", " ", raw[: season_position.start()])
            leading_cjk = re.match(
                r"^\s*([\u3400-\u9fff][\u3400-\u9fff：:·、，,\-— ]{1,80})",
                before_season,
            )
            if leading_cjk:
                cjk_title = _clean_text(leading_cjk.group(1), maximum=120)
    english_title = _clean_text(prefix, maximum=300)
    if not has_latin(english_title):
        english_title = ""
    if not english_title and season:
        season_position = RELEASE_SEASON_MARK_RE.search(raw)
        if season_position:
            after_season = raw[season_position.end() :]
            after_prefix, _, _ = _release_prefix(after_season)
            if has_latin(after_prefix):
                english_title = _clean_text(after_prefix, maximum=300)
    english_title = re.sub(r"^[\[【（(]+|[\]】）)]+$", "", english_title)
    return cjk_title, english_title, year, season


def _metadata_resolution_query(item: dict[str, Any]) -> str:
    """Build a stable resolver query while retaining the original cache key."""

    raw_query = _metadata_query(item)
    cjk_title, english_title, parsed_year, season = _parse_release_label(
        raw_query
    )
    year = parsed_year or _clean_text(item.get("year"), maximum=4)
    parts = [part for part in (cjk_title, english_title, year, season) if part]
    canonical = _clean_text(" ".join(parts), maximum=2000)
    return canonical or raw_query


def _matching_qb_release_name(item: dict[str, Any]) -> str:
    title = re.sub(
        r"\s*[\[(（]?(?:19|20)\d{2}[\])）]?\s*$",
        "",
        _clean_text(item.get("title"), maximum=300),
    ).strip()
    english = re.sub(
        r"\s*[\[(（]?(?:19|20)\d{2}[\])）]?\s*$",
        "",
        _clean_text(item.get("englishTitle"), maximum=300),
    ).strip()
    title_key = _release_normalized(title)
    english_key = _release_normalized(english)
    item_years = {
        *YEAR_RE.findall(str(item.get("year") or "")),
        *YEAR_RE.findall(str(item.get("title") or "")),
        *YEAR_RE.findall(str(item.get("englishTitle") or "")),
    }
    matches: dict[str, str] = {}
    for task in (item.get("_private") or {}).get("qbTasks") or []:
        name = _clean_text(task.get("name"), maximum=2000)
        if not name:
            continue
        task_years = set(YEAR_RE.findall(name))
        if item_years and task_years and item_years.isdisjoint(task_years):
            continue
        name_key = _release_normalized(name)
        title_matches = has_cjk(title) and title_key and title_key in name_key
        english_matches = (
            has_latin(english)
            and english_key
            and english_key in name_key
        )
        if title_matches or english_matches:
            matches.setdefault(name_key, name)
    return next(iter(matches.values())) if len(matches) == 1 else ""


def _metadata_query(item: dict[str, Any]) -> str:
    if item.get("library") and needs_bilingual_name(item):
        release_name = _matching_qb_release_name(item)
        if release_name:
            return release_name
    english = _clean_text(item.get("englishTitle"), maximum=2000)
    title = _clean_text(item.get("title"), maximum=2000)
    if has_latin(english):
        return english
    if has_latin(title):
        return title
    return english or title


def needs_bilingual_name(item: dict[str, Any]) -> bool:
    """Return whether the public title is still too weak to identify.

    A clear Chinese library title is already useful for human review even
    when Jellyfin/MoviePilot did not provide a separate English/original
    title. Requiring two languages here incorrectly marked rows such as
    ``X战警`` as unresolved. qB-only rows still need a separate Latin title
    unless their metadata resolver supplied one, because a Chinese release
    label can contain quality or uploader text rather than a media identity.
    Identity conflicts are handled independently by ``_merge_group``.
    """

    title = _clean_text(item.get("title"), maximum=300)
    english = _clean_text(item.get("englishTitle"), maximum=300)
    return (
        not has_cjk(title)
        or "待识别" in title
        or "待核" in title
        or (
            not bool(item.get("library"))
            and (title == english or not has_latin(english))
        )
    )


def bilingual_name_verified(item: dict[str, Any]) -> bool:
    """A cleanup row is identifiable when its title is sufficiently clear."""

    if (
        item.get("metadataResolved") is True
        and has_latin(item.get("title"))
        and has_latin(item.get("englishTitle"))
        and not any(
            marker in str(item.get("title") or "")
            for marker in ("待识别", "待核")
        )
    ):
        return True
    return not needs_bilingual_name(item)


def _task_is_unfinished(task: dict[str, Any]) -> bool:
    try:
        return float(task.get("progress") or 0) < 1
    except (TypeError, ValueError):
        return True


def _task_display_status(task: dict[str, Any]) -> tuple[str, str]:
    """Keep the public task label aligned with qB's download/upload state."""

    state = str(task.get("state") or "").casefold()
    if task.get("selfPublish"):
        return "自发布", "warning"
    if task.get("hr"):
        return "H&R 保护", "protected"
    if task.get("hrUnknown"):
        return "H&R 待核", "protected"
    if state in DOWNLOAD_STATES or _task_is_unfinished(task):
        return "下载中", "protected"
    if state.startswith("stopped") or state == "pausedup":
        return "已停止", "normal"
    return "做种中", "normal"


def make_hr_metadata_resources(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result = []
    for raw in records:
        if not isinstance(raw, dict):
            continue
        record_id = _clean_text(raw.get("id"), maximum=32)
        title = _clean_text(raw.get("title"), maximum=2000)
        if not record_id.isdigit() or not title:
            continue
        kind = "tv" if SEASON_RANGE_RE.search(title) else "movie"
        year_match = YEAR_RE.search(title)
        result.append(
            {
                "id": f"hr_{record_id}",
                "title": "中文名待识别",
                "englishTitle": title,
                "edition": "H&R 待恢复",
                "type": "电视剧" if kind == "tv" else "电影",
                "year": year_match.group(1) if year_match else "",
                "library": False,
                "_private": {
                    "identity": f"hr:{record_id}",
                    "qbTasks": [],
                },
            }
        )
    return result


def _hr_scope(value: object) -> str:
    text = str(value or "")
    season = re.search(r"(?i)\bS(\d{1,2})(?:E(\d{1,3}))?", text)
    quality = re.search(r"(?i)\b(\d{3,4}[pi]|4K)\b", text)
    parts = []
    if season:
        parts.append(
            f"S{int(season.group(1)):02d}"
            + (
                f"E{int(season.group(2)):02d}"
                if season.group(2)
                else ""
            )
        )
    if quality:
        parts.append(quality.group(1).upper().replace("P", "p"))
    return " · ".join(parts) or "范围待核"


def _resource_video_sizes(resource: dict[str, Any]) -> tuple[int, ...]:
    files = (resource.get("_private") or {}).get("files") or []
    by_inode: dict[tuple[int, int], int] = {}
    for item in files:
        if not isinstance(item, dict):
            continue
        try:
            key = (int(item.get("dev") or 0), int(item.get("inode") or 0))
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            continue
        if key[0] <= 0 or key[1] <= 0 or size <= 0:
            continue
        by_inode[key] = size
    return tuple(sorted(by_inode.values()))


def annotate_hr_missing_resources(
    resources: list[dict[str, Any]],
    records: list[dict[str, Any]],
    cache: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    sanitized = sanitize_metadata_cache(cache)
    entries = sanitized["entries"]
    pseudos = {
        item["id"].removeprefix("hr_"): item
        for item in make_hr_metadata_resources(records)
    }
    by_identity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_name: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for resource in resources:
        private = resource.get("_private") or {}
        identity = str(private.get("identity") or "")
        if identity:
            by_identity[identity].append(resource)
        kind = _media_kind(resource)
        for value in (resource.get("title"), resource.get("englishTitle")):
            normalized = _release_normalized(value)
            if normalized:
                by_name[(kind, normalized)].append(resource)

    annotated_records: list[dict[str, Any]] = []
    linked_resource_ids: set[str] = set()
    linked_records = 0
    for raw in records:
        if not isinstance(raw, dict):
            continue
        record = {
            "id": _clean_text(raw.get("id"), maximum=32),
            "title": _clean_text(raw.get("title"), maximum=2000),
            "coveredByCandidate": bool(raw.get("coveredByCandidate")),
            "qbTaskPresent": bool(raw.get("qbTaskPresent")),
        }
        video_sizes = raw.get("videoSizes")
        if isinstance(video_sizes, list):
            record["videoSizes"] = sorted(
                [
                    int(size)
                    for size in video_sizes
                    if isinstance(size, int) and size > 0
                ]
            )
        pseudo = pseudos.get(record["id"])
        candidates: list[dict[str, Any]] = []
        if pseudo:
            if record.get("videoSizes"):
                expected_sizes = tuple(record["videoSizes"])
                candidates.extend(
                    resource
                    for resource in resources
                    if _resource_video_sizes(resource) == expected_sizes
                )
            else:
                entry = entries.get(metadata_cache_key(pseudo))
                if entry and entry.get("status") == "resolved":
                    identity = str(entry.get("identity") or "")
                    candidates.extend(by_identity.get(identity, []))
                    if not candidates:
                        for value in (
                            entry.get("title"),
                            entry.get("englishTitle"),
                        ):
                            candidates.extend(
                                by_name.get(
                                    (
                                        str(entry.get("kind") or ""),
                                        _release_normalized(value),
                                    ),
                                    [],
                                )
                            )
                if not candidates:
                    release_key = _release_normalized(record["title"])
                    for (kind, name_key), matching in by_name.items():
                        if kind == _media_kind(pseudo) and (
                            release_key.startswith(name_key + " ")
                            or release_key == name_key
                        ):
                            candidates.extend(matching)
        unique = {
            str(candidate["id"]): candidate for candidate in candidates
        }
        linked = next(iter(unique.values())) if len(unique) == 1 else None
        if linked:
            linked_records += 1
            linked_resource_ids.add(str(linked["id"]))
            record["linkedResourceTitle"] = str(linked.get("title") or "")
            linked["hrPending"] = True
            linked["protected"] = True
            existing_lock_reason = str(linked.get("lockReason") or "")
            lock_reason_parts = [
                part for part in existing_lock_reason.split("；") if part
            ]
            if "H&R 保护" not in lock_reason_parts:
                lock_reason_parts.append("H&R 保护")
            linked["lockReason"] = "；".join(lock_reason_parts)
            if record["qbTaskPresent"]:
                linked["impactTitle"] = "学校站 H&R 下载未完成"
                linked["impactDetail"] = (
                    "官方任务已在 qB，但 payload 尚未完成；"
                    "100% 重检通过前禁止清理"
                )
            else:
                linked["impactTitle"] = "学校站 H&R 任务缺失"
                linked["impactDetail"] = (
                    "媒体文件仍在，但官方 H&R 任务不在 qB；"
                    "恢复并精确重检前禁止清理"
                )
            task = {
                "site": "学校站",
                "scope": _hr_scope(record["title"]),
                "status": (
                    "H&R 未完成"
                    if record["qbTaskPresent"]
                    else "H&R 缺失"
                ),
                "tone": "protected",
            }
            seed_tasks = list(linked.get("seedTasks") or [])
            if task not in seed_tasks:
                seed_tasks.append(task)
            linked["seedTasks"] = seed_tasks
            private = linked.get("_private") or {}
            missing_ids = set(private.get("hrMissingIds") or [])
            missing_ids.add(record["id"])
            private["hrMissingIds"] = sorted(missing_ids)
            linked["_private"] = private
        annotated_records.append(record)
    return resources, annotated_records, {
        "hrMissingLinkedRecords": linked_records,
        "hrMissingLinkedResources": len(linked_resource_ids),
        "hrMissingUnassigned": sum(
            not record.get("linkedResourceTitle")
            for record in annotated_records
        ),
    }


def metadata_cache_key(item: dict[str, Any]) -> str:
    query = _metadata_query(item)
    value = f"{_media_kind(item)}\0{query.casefold()}"
    hint = _identity_hint(item)
    if hint:
        value += f"\0{hint}"
    payload = value.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sanitize_metadata_cache(value: object) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or value.get("version") != CACHE_VERSION:
        return {"version": CACHE_VERSION, "entries": {}}
    raw_entries = value.get("entries")
    if not isinstance(raw_entries, dict) or len(raw_entries) > 5000:
        return {"version": CACHE_VERSION, "entries": {}}
    entries: dict[str, dict[str, Any]] = {}
    for raw_key, raw_entry in raw_entries.items():
        key = str(raw_key).lower()
        if (
            len(key) != 64
            or any(char not in "0123456789abcdef" for char in key)
            or not isinstance(raw_entry, dict)
        ):
            continue
        query = _clean_text(raw_entry.get("query"), maximum=2000)
        kind = raw_entry.get("kind")
        status = raw_entry.get("status")
        checked_at = _clean_text(raw_entry.get("checkedAt"), maximum=64)
        if not query or kind not in {"movie", "tv"}:
            continue
        if status == "unresolved":
            entry = {
                "query": query,
                "kind": kind,
                "status": status,
                "checkedAt": checked_at,
            }
            parsed_english = _clean_text(
                raw_entry.get("parsedEnglish"),
                maximum=300,
            )
            parsed_year = _clean_text(
                raw_entry.get("parsedYear"),
                maximum=4,
            )
            if parsed_english and has_latin(parsed_english):
                entry["parsedEnglish"] = parsed_english
            if parsed_year:
                entry["parsedYear"] = parsed_year
            entries[key] = entry
            continue
        if status != "resolved":
            continue
        title = _clean_text(raw_entry.get("title"), maximum=300)
        english_title = _clean_text(
            raw_entry.get("englishTitle"),
            maximum=300,
        )
        year = _clean_text(raw_entry.get("year"), maximum=4)
        identity = _clean_text(raw_entry.get("identity"), maximum=300)
        tmdb_id = raw_entry.get("tmdbId")
        provider_verified = raw_entry.get("providerVerified") is True
        provider_title = (
            isinstance(tmdb_id, int)
            and tmdb_id > 0
            and has_latin(title)
            and provider_verified
        )
        if (
            not title
            or (not has_cjk(title) and not provider_title)
            or not english_title
            or not has_latin(english_title)
            or not identity
        ):
            continue
        if tmdb_id is not None and (
            not isinstance(tmdb_id, int) or tmdb_id <= 0
        ):
            continue
        entries[key] = {
            "query": query,
            "kind": kind,
            "status": status,
            "checkedAt": checked_at,
            "title": title,
            "englishTitle": english_title,
            "year": year,
            "identity": identity,
            "tmdbId": tmdb_id,
            "providerVerified": provider_verified,
        }
    return {"version": CACHE_VERSION, "entries": entries}


def validate_metadata_overrides(
    value: object,
) -> dict[tuple[str, str], dict[str, str]]:
    """Validate audited, exact-query name overrides and fail closed."""

    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("metadata overrides must use version 1")
    raw_entries = value.get("entries")
    if not isinstance(raw_entries, list) or len(raw_entries) > 256:
        raise ValueError("metadata override entries must be a bounded list")
    result: dict[tuple[str, str], dict[str, str]] = {}
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise ValueError(f"metadata override {index} is not an object")
        entry = {
            key: _clean_text(raw.get(key), maximum=maximum)
            for key, maximum in (
                ("query", 2000),
                ("kind", 8),
                ("title", 300),
                ("englishTitle", 300),
                ("year", 4),
                ("identity", 100),
                ("source", 1000),
                ("verifiedAt", 32),
            )
        }
        if (
            entry["kind"] not in {"movie", "tv"}
            or not entry["query"]
            or not has_cjk(entry["title"])
            or not has_latin(entry["englishTitle"])
            or not re.fullmatch(r"(?:19|20)\d{2}", entry["year"])
            or not MANUAL_IDENTITY_RE.fullmatch(entry["identity"])
            or not entry["identity"].startswith(entry["kind"] + ":")
            or not entry["source"].startswith("https://")
        ):
            raise ValueError(f"metadata override {index} is invalid")
        try:
            verified_at = datetime.fromisoformat(entry["verifiedAt"])
        except ValueError as exc:
            raise ValueError(
                f"metadata override {index} has invalid verifiedAt"
            ) from exc
        if verified_at.tzinfo is None:
            raise ValueError(
                f"metadata override {index} verifiedAt lacks timezone"
            )
        key = (entry["kind"], entry["query"])
        if key in result:
            raise ValueError(
                f"duplicate metadata override: {entry['kind']} {entry['query']}"
            )
        result[key] = entry
    return result


def apply_metadata_overrides(
    resources: list[dict[str, Any]],
    cache: dict[str, Any],
    overrides: object,
) -> tuple[dict[str, Any], int]:
    """Apply only exact, year-compatible audited mappings."""

    sanitized = sanitize_metadata_cache(cache)
    validated = validate_metadata_overrides(overrides)
    applied: set[str] = set()
    for item in resources:
        query = _metadata_query(item)
        kind = _media_kind(item)
        override = validated.get((kind, query))
        if not override:
            continue
        requested_years = {
            *YEAR_RE.findall(query),
            *YEAR_RE.findall(str(item.get("year") or "")),
        }
        if requested_years and override["year"] not in requested_years:
            continue
        key = metadata_cache_key(item)
        sanitized["entries"][key] = {
            "query": query,
            "kind": kind,
            "status": "resolved",
            "checkedAt": override["verifiedAt"],
            "title": override["title"],
            "englishTitle": override["englishTitle"],
            "year": override["year"],
            "identity": override["identity"],
            "tmdbId": None,
        }
        applied.add(key)
    return sanitize_metadata_cache(sanitized), len(applied)


def prune_metadata_cache(
    resources: list[dict[str, Any]],
    cache: dict[str, Any],
) -> dict[str, Any]:
    """Keep only entries addressable by the current inventory."""

    sanitized = sanitize_metadata_cache(cache)
    active_keys = {
        metadata_cache_key(item)
        for item in resources
        if _metadata_query(item)
    }
    sanitized["entries"] = {
        key: entry
        for key, entry in sanitized["entries"].items()
        if key in active_keys
    }
    return sanitized


def _retry_due(entry: dict[str, Any], now: datetime) -> bool:
    if entry.get("status") != "unresolved":
        return False
    try:
        checked_at = datetime.fromisoformat(str(entry.get("checkedAt") or ""))
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return checked_at.astimezone(timezone.utc) + RETRY_AFTER <= now


def _resolved_alias_entry(
    entries: dict[str, dict[str, Any]],
    item: dict[str, Any],
) -> dict[str, Any] | None:
    """Reuse a confirmed identity for a new quality/site release variant."""

    target = _metadata_resolution_query(item).casefold()
    kind = _media_kind(item)
    type_label = "电视剧" if kind == "tv" else "电影"
    for entry in entries.values():
        if entry.get("status") != "resolved" or entry.get("kind") != kind:
            continue
        alias_query = _metadata_resolution_query(
            {
                "type": type_label,
                "library": False,
                "englishTitle": entry.get("query"),
            }
        ).casefold()
        if alias_query == target:
            return copy.deepcopy(entry)
    return None


def _resolver_source(items: list[dict[str, Any]], workers: int) -> str:
    encoded = base64.b64encode(
        json.dumps(items, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    return f"""
import base64
import concurrent.futures
import hashlib
import json
import re
from app.chain.media import MediaChain
from app.chain.douban import DoubanChain
from app.core.metainfo import MetaInfo
from app.schemas.types import MediaType

ITEMS = json.loads(base64.b64decode({encoded!r}).decode("utf-8"))
chain = MediaChain()

def has_cjk(value):
    return any("\\u3400" <= char <= "\\u9fff" for char in str(value or ""))

def clean(value):
    return re.sub(r"\\s+", " ", str(value or "")).strip()

def normalized(value):
    return re.sub(r"[^0-9A-Za-z\\u3400-\\u9fff]+", "", clean(value)).casefold()

def parsed(item):
    try:
        meta = MetaInfo(item.get("resolutionQuery") or item["query"])
    except Exception:
        meta = None
    parsed_cn = clean(getattr(meta, "cn_name", ""))
    parsed_en = clean(getattr(meta, "en_name", "") or getattr(meta, "name", ""))
    year = clean(getattr(meta, "year", ""))
    if not parsed_cn:
        parsed_cn = clean(item.get("parsedChinese", ""))
    if not parsed_en:
        parsed_en = clean(item.get("parsedEnglish", ""))
    if not year:
        year = clean(item.get("parsedYear", ""))
    if not parsed_cn:
        prefix = re.match(r"^\\s*[\\[【]([^\\]】]+)[\\]】]", item["query"])
        if prefix and has_cjk(prefix.group(1)):
            parsed_cn = clean(prefix.group(1))
    return meta, parsed_cn, parsed_en, year

def info_record(record, info):
    season_years = getattr(info, "season_years", {{}}) or {{}}
    return {{
        "key": record["key"],
        "query": record["query"],
        "kind": record["kind"],
        "status": "recognized",
        "parsedChinese": record.get("parsedChinese", ""),
        "parsedEnglish": record.get("parsedEnglish", ""),
        "parsedYear": record.get("parsedYear", ""),
        "parsedSeason": record.get("parsedSeason"),
        "title": clean(getattr(info, "title", "")),
        "originalTitle": clean(getattr(info, "original_title", "")),
        "englishTitle": clean(getattr(info, "en_title", "")),
        "year": clean(getattr(info, "year", "")),
        "resultType": str(getattr(info, "type", "")),
        "tmdbId": getattr(info, "tmdb_id", None),
        "imdbId": clean(getattr(info, "imdb_id", "")),
        "seasonYears": {{
            str(key): clean(value) for key, value in season_years.items()
        }},
    }}

def douban_fallback(record):
    # Use MoviePilot's configured Douban search when the primary chain misses.
    try:
        kind = record["kind"]
        mtype = MediaType.TV if kind == "tv" else MediaType.MOVIE
        target_names = {{
            normalized(value)
            for value in (
                record.get("parsedChinese"),
                record.get("parsedEnglish"),
                getattr(record.get("_meta"), "name", ""),
            )
            if normalized(value)
        }}
        year = clean(record.get("parsedYear", ""))
        candidates = []
        for name in (
            record.get("parsedChinese"),
            record.get("parsedEnglish"),
            getattr(record.get("_meta"), "name", ""),
        ):
            name = clean(name)
            if not name:
                continue
            probe = MetaInfo(name)
            probe.type = mtype
            for info in (DoubanChain().search_medias(probe) or []):
                info_type = str(getattr(info, "type", "")).casefold()
                if (kind == "tv" and "tv" not in info_type) or (
                    kind == "movie" and "movie" not in info_type
                ):
                    continue
                names = [
                    getattr(info, "title", ""),
                    getattr(info, "original_title", ""),
                    getattr(info, "en_title", ""),
                    *(getattr(info, "names", []) or []),
                ]
                exact = any(normalized(value) in target_names for value in names)
                info_year = clean(getattr(info, "year", ""))
                year_match = bool(year and info_year == year)
                if exact and (not year or year_match):
                    candidates.append((int(year_match), int(exact), info))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        identities = {{
            str(getattr(info, "tmdb_id", ""))
            for _, _, info in candidates
            if getattr(info, "tmdb_id", None)
        }}
        if len(identities) != 1:
            return None
        return info_record(record, candidates[0][2])
    except Exception:
        return None

prepared = []
network_items = []
for item in ITEMS:
    try:
        meta, parsed_cn, parsed_en, year = parsed(item)
        record = {{
            "key": item["key"],
            "query": item["query"],
            "kind": item["kind"],
            "parsedChinese": parsed_cn,
            "parsedEnglish": parsed_en,
            "parsedYear": year,
            "parsedSeason": getattr(meta, "begin_season", None),
            "tmdbId": item.get("tmdbId"),
        }}
        if (
            not item.get("tmdbId")
            and parsed_cn
            and has_cjk(parsed_cn)
            and parsed_en
            and re.search(r"[A-Za-z]", parsed_en)
        ):
            record["status"] = "parsed"
            prepared.append(record)
        else:
            record["_meta"] = meta
            network_items.append(record)
    except Exception as exc:
        prepared.append({{
            "key": item["key"],
            "query": item["query"],
            "kind": item["kind"],
            "status": "unresolved",
        }})

def recognize(record):
    try:
        kind = record["kind"]
        info = chain.recognize_media(
            meta=record["_meta"],
            mtype=MediaType.TV if kind == "tv" else MediaType.MOVIE,
            tmdbid=record.get("tmdbId"),
            cache=True,
        )
        if not info:
            fallback = douban_fallback(record)
            record.pop("_meta", None)
            if fallback:
                return fallback
            record["status"] = "unresolved"
            return record
        record.update({{
            "status": "recognized",
            "title": clean(getattr(info, "title", "")),
            "originalTitle": clean(getattr(info, "original_title", "")),
            "englishTitle": clean(getattr(info, "en_title", "")),
            "year": clean(getattr(info, "year", "")),
            "resultType": str(getattr(info, "type", "")),
            "tmdbId": getattr(info, "tmdb_id", None),
            "imdbId": clean(getattr(info, "imdb_id", "")),
            "seasonYears": {{
                str(key): clean(value)
                for key, value in (getattr(info, "season_years", {{}}) or {{}}).items()
            }},
        }})
    except Exception:
        pass
    if record.get("status") != "recognized":
        fallback = douban_fallback(record)
        if fallback:
            return fallback
        record["status"] = "unresolved"
    record.pop("_meta", None)
    return record

with concurrent.futures.ThreadPoolExecutor(max_workers={workers}) as pool:
    prepared.extend(pool.map(recognize, network_items))

print({RESULT_SENTINEL!r} + json.dumps(prepared, ensure_ascii=False, default=str))
"""


def _validated_result(
    item: dict[str, Any],
    result: dict[str, Any],
    *,
    checked_at: str,
) -> dict[str, Any]:
    query = _metadata_query(item)
    kind = _media_kind(item)
    parsed_english = _clean_text(
        result.get("parsedEnglish"),
        maximum=300,
    )
    parsed_year = _clean_text(result.get("parsedYear"), maximum=4)
    base = {
        "query": query,
        "kind": kind,
        "status": "unresolved",
        "checkedAt": checked_at,
    }
    if parsed_english and has_latin(parsed_english):
        base["parsedEnglish"] = parsed_english
    if parsed_year:
        base["parsedYear"] = parsed_year
    if (
        result.get("query") != query
        or result.get("kind") != kind
        or result.get("status")
        not in {"parsed", "recognized"}
    ):
        return base
    parsed_cn = _clean_text(result.get("parsedChinese"), maximum=300)
    parsed_en = parsed_english
    if result.get("status") == "parsed":
        if (
            not has_cjk(parsed_cn)
            or not parsed_en
            or not has_latin(parsed_en)
        ):
            return base
        identity_source = (
            f"{kind}\0{parsed_en.casefold()}\0{parsed_year}"
        ).encode("utf-8")
        return {
            **base,
            "status": "resolved",
            "title": parsed_cn,
            "englishTitle": parsed_en,
            "year": parsed_year,
            "identity": (
                f"{kind}:name:"
                + hashlib.sha256(identity_source).hexdigest()[:24]
            ),
            "tmdbId": None,
        }
    title = _clean_text(result.get("title"), maximum=300)
    english_title = _clean_text(
        result.get("englishTitle"),
        maximum=300,
    )
    if not has_latin(english_title):
        english_title = (
            parsed_en
            if has_latin(parsed_en)
            else _clean_text(
                result.get("originalTitle"),
                maximum=300,
            )
        )
    year = _clean_text(result.get("year"), maximum=4) or parsed_year
    tmdb_id = result.get("tmdbId")
    result_type = str(result.get("resultType") or "").casefold()
    requested_year = parsed_year or _clean_text(item.get("year"), maximum=4)
    requested_years = {requested_year} if requested_year else set()
    parsed_season = result.get("parsedSeason")
    season_years = result.get("seasonYears")
    season_year = ""
    if parsed_season is not None and isinstance(season_years, dict):
        season_year = _clean_text(
            season_years.get(str(parsed_season)),
            maximum=4,
        )
    year_compatible = not requested_years or year in requested_years
    if kind == "tv" and season_year and season_year in requested_years:
        # A season release year (e.g. S04=2026) legitimately differs from
        # the parent series premiere year (e.g. 2018).  Validate against the
        # exact season when MoviePilot provides it instead of rejecting a
        # correct identity as a stale-year conflict.
        year_compatible = True
    provider_title = (
        isinstance(tmdb_id, int)
        and tmdb_id > 0
        and has_latin(title)
        and bool(_identity_hint(item))
    )
    if (
        not title
        or (not has_cjk(title) and not provider_title)
        or not english_title
        or not has_latin(english_title)
        or not isinstance(tmdb_id, int)
        or tmdb_id <= 0
        or (kind == "tv" and "tv" not in result_type)
        or (kind == "movie" and "movie" not in result_type)
        or (not _identity_hint(item) and not year_compatible)
    ):
        return base
    return {
        **base,
        "status": "resolved",
        "title": title,
        "englishTitle": english_title,
        "year": year,
        "identity": f"{kind}:tmdb:{tmdb_id}",
        "tmdbId": tmdb_id,
        "providerVerified": provider_title,
    }


def resolve_media_names(
    *,
    host: str | None,
    resources: list[dict[str, Any]],
    cache: dict[str, Any],
    limit: int = 128,
    workers: int = 8,
    timeout: int = 150,
    retry_unresolved: bool = False,
) -> tuple[dict[str, Any], bool]:
    sanitized = sanitize_metadata_cache(cache)
    entries = sanitized["entries"]
    now = datetime.now(timezone.utc)
    candidates: list[dict[str, Any]] = []
    source_by_key: dict[str, dict[str, Any]] = {}
    for item in resources:
        if item.get("library") and not needs_bilingual_name(item):
            continue
        key = metadata_cache_key(item)
        entry = entries.get(key)
        if entry and entry.get("status") == "resolved":
            continue
        if not entry:
            alias_entry = _resolved_alias_entry(entries, item)
            if alias_entry:
                alias_entry["query"] = _metadata_query(item)
                alias_entry["checkedAt"] = alias_entry.get(
                    "checkedAt", now.isoformat(timespec="seconds")
                )
                entries[key] = alias_entry
                continue
        if (
            entry
            and not retry_unresolved
            and not _retry_due(entry, now)
        ):
            continue
        if key in source_by_key:
            continue
        query = _metadata_query(item)
        if not query:
            continue
        source_by_key[key] = item
        parsed_cn, parsed_en, parsed_year, _season = _parse_release_label(
            query
        )
        candidate = {
            "key": key,
            "query": query,
            "resolutionQuery": _metadata_resolution_query(item),
            "kind": _media_kind(item),
            "parsedChinese": parsed_cn,
            "parsedEnglish": parsed_en,
            "parsedYear": parsed_year,
        }
        hint = _identity_hint(item)
        if hint:
            candidate["tmdbId"] = int(hint.rsplit(":", 1)[1])
        candidates.append(candidate)
        if len(candidates) >= max(0, limit):
            break
    if not candidates:
        return sanitized, True
    source = _resolver_source(candidates, max(1, min(workers, 12)))
    command = [
        "sudo",
        "-n",
        "docker",
        "exec",
        "-i",
        "moviepilot-v2-pilot",
        "python",
        "-",
    ]
    if host is not None:
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            host,
            *command,
        ]
    try:
        completed = subprocess.run(
            command,
            input=source,
            text=True,
            capture_output=True,
            check=True,
            timeout=timeout,
        )
        result_line = next(
            line
            for line in reversed(completed.stdout.splitlines())
            if line.startswith(RESULT_SENTINEL)
        )
        raw_results = json.loads(result_line[len(RESULT_SENTINEL) :])
        if not isinstance(raw_results, list):
            raise ValueError("metadata resolver returned a non-list")
    except (
        OSError,
        StopIteration,
        ValueError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ):
        return sanitized, False
    checked_at = now.isoformat(timespec="seconds")
    result_by_key = {
        str(item.get("key") or ""): item
        for item in raw_results
        if isinstance(item, dict)
    }
    for key, source_item in source_by_key.items():
        entries[key] = _validated_result(
            source_item,
            result_by_key.get(key, {}),
            checked_at=checked_at,
        )
    tmdb_aliases: dict[
        tuple[str, str, str],
        set[str],
    ] = defaultdict(set)
    for entry in entries.values():
        if (
            entry.get("status") == "resolved"
            and entry.get("tmdbId")
        ):
            tmdb_aliases[
                (
                    str(entry["kind"]),
                    str(entry["englishTitle"]).casefold(),
                    str(entry.get("year") or ""),
                )
            ].add(str(entry["identity"]))
    for entry in entries.values():
        if entry.get("status") != "resolved":
            continue
        identities = tmdb_aliases.get(
            (
                str(entry["kind"]),
                str(entry["englishTitle"]).casefold(),
                str(entry.get("year") or ""),
            ),
            set(),
        )
        if len(identities) == 1:
            entry["identity"] = next(iter(identities))
            if entry.get("tmdbId") is None:
                entry["tmdbId"] = int(entry["identity"].rsplit(":", 1)[1])
    aliases: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(
        list
    )
    aliases_without_year: dict[
        tuple[str, str],
        list[dict[str, Any]],
    ] = defaultdict(list)
    for entry in entries.values():
        if entry.get("status") != "resolved":
            continue
        alias = (
            str(entry["kind"]),
            str(entry["englishTitle"]).casefold(),
            str(entry.get("year") or ""),
        )
        aliases[alias].append(entry)
        aliases_without_year[
            (str(entry["kind"]), str(entry["englishTitle"]).casefold())
        ].append(entry)
    for key, entry in list(entries.items()):
        if entry.get("status") != "unresolved":
            continue
        parsed_english = str(entry.get("parsedEnglish") or "")
        if not parsed_english:
            continue
        parsed_year = str(entry.get("parsedYear") or "")
        matches = (
            aliases.get(
                (
                    str(entry["kind"]),
                    parsed_english.casefold(),
                    parsed_year,
                ),
                [],
            )
            if parsed_year
            else aliases_without_year.get(
                (str(entry["kind"]), parsed_english.casefold()),
                [],
            )
        )
        identities = {match["identity"] for match in matches}
        if len(identities) != 1:
            continue
        match = matches[0]
        entries[key] = {
            **match,
            "query": entry["query"],
            "checkedAt": checked_at,
        }
    return sanitize_metadata_cache(sanitized), True


def _season_numbers(value: object) -> set[int]:
    result: set[int] = set()
    for match in SEASON_RANGE_RE.finditer(str(value or "")):
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if 0 <= start <= end <= 99 and end - start <= 30:
            result.update(range(start, end + 1))
    return result


def _season_label(values: set[int]) -> str:
    seasons = sorted(values)
    if not seasons:
        return ""
    if len(seasons) == 1:
        return f"S{seasons[0]:02d}"
    if seasons == list(range(seasons[0], seasons[-1] + 1)):
        return f"S{seasons[0]:02d}–S{seasons[-1]:02d}"
    return "、".join(f"S{value:02d}" for value in seasons)


def _episode_gap_values(values: set[str]) -> set[str]:
    """Return local leading/interior gaps from a merged episode union."""

    by_season: dict[int, set[int]] = defaultdict(set)
    for value in values:
        match = EPISODE_VALUE_RE.fullmatch(str(value).strip())
        if not match:
            continue
        season = int(match.group(1))
        episode = int(match.group(2))
        if season >= 1 and episode >= 1:
            by_season[season].add(episode)
    result: set[str] = set()
    for season, episodes in by_season.items():
        result.update(
            f"S{season:02d}E{episode:02d}"
            for episode in range(1, max(episodes) + 1)
            if episode not in episodes
        )
    return result


def _human_size(gib: float) -> str:
    if gib >= 1024:
        return f"{gib / 1024:.2f} TB"
    return f"{gib:.1f} GB"


def _strict_unique(
    records: list[dict[str, Any]],
    *,
    key_name: str,
) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        key = str(record.get(key_name) or "")
        if not key:
            continue
        existing = result.get(key)
        if existing is not None and existing != record:
            raise ValueError(f"conflicting {key_name} record: {key}")
        result[key] = copy.deepcopy(record)
    return [result[key] for key in sorted(result)]


def _merge_cleanup_files(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    fingerprint_fields = (
        "allowed",
        "exists",
        "regular",
        "dev",
        "inode",
        "size",
        "nlink",
    )
    for record in records:
        path = str(record.get("path") or "")
        if not path:
            continue
        existing = result.get(path)
        if existing is None:
            result[path] = copy.deepcopy(record)
            continue
        if any(
            field in existing
            and field in record
            and existing[field] != record[field]
            for field in fingerprint_fields
        ):
            raise ValueError(f"conflicting cleanup fingerprint: {path}")
        existing["source"] = (
            "library"
            if "library" in {existing.get("source"), record.get("source")}
            else existing.get("source") or record.get("source")
        )
        existing["required"] = bool(
            existing.get("required") or record.get("required")
        )
        existing["relativeSafe"] = bool(
            existing.get("relativeSafe", True)
            and record.get("relativeSafe", True)
        )
        if "qbExpectedSize" in record:
            existing["qbExpectedSize"] = max(
                int(existing.get("qbExpectedSize") or 0),
                int(record["qbExpectedSize"]),
            )
        if "qbProgress" in record:
            existing["qbProgress"] = min(
                float(existing.get("qbProgress", 1)),
                float(record["qbProgress"]),
            )
    return [result[path] for path in sorted(result)]


def _links_complete(records: list[dict[str, Any]]) -> bool:
    paths_by_inode: dict[tuple[int, int], set[str]] = defaultdict(set)
    expected: dict[tuple[int, int], int] = {}
    for record in records:
        try:
            inode = (int(record["dev"]), int(record["inode"]))
            nlink = int(record["nlink"])
            path = str(record["path"])
        except (KeyError, TypeError, ValueError):
            return False
        if inode in expected and expected[inode] != nlink:
            return False
        expected[inode] = nlink
        paths_by_inode[inode].add(path)
    return bool(records) and all(
        len(paths_by_inode[inode]) >= nlink and nlink > 0
        for inode, nlink in expected.items()
    )


def _cleanup_links_complete(records: list[dict[str, Any]]) -> bool:
    existing = [record for record in records if record.get("exists")]
    if not existing or any(not record.get("regular") for record in existing):
        return False
    paths_by_inode: dict[tuple[int, int], set[str]] = defaultdict(set)
    expected: dict[tuple[int, int], int] = {}
    for record in existing:
        try:
            inode = (int(record["dev"]), int(record["inode"]))
            nlink = int(record["nlink"])
            path = str(record["path"])
        except (KeyError, TypeError, ValueError):
            return False
        if inode in expected and expected[inode] != nlink:
            return False
        expected[inode] = nlink
        paths_by_inode[inode].add(path)
    return all(
        len(paths_by_inode[inode]) == nlink and nlink > 0
        for inode, nlink in expected.items()
    )


def _merge_private(
    items: list[dict[str, Any]],
    identity: str,
) -> dict[str, Any]:
    private_records = [item["_private"] for item in items]
    files = _strict_unique(
        [
            record
            for private in private_records
            for record in private.get("files") or []
        ],
        key_name="path",
    )
    cleanup_files = _merge_cleanup_files(
        [
            record
            for private in private_records
            for record in private.get("cleanupFiles") or []
        ]
    )
    roots = _strict_unique(
        [
            record
            for private in private_records
            for record in private.get("roots") or []
        ],
        key_name="path",
    )
    qb_tasks = _strict_unique(
        [
            record
            for private in private_records
            for record in private.get("qbTasks") or []
        ],
        key_name="hash",
    )
    moviepilot_indexes = _strict_unique(
        [
            record
            for private in private_records
            for record in private.get("moviepilotIndexes") or []
        ],
        key_name="id",
    )
    return {
        "identity": identity,
        "allLinksKnown": _links_complete(files),
        "files": files,
        "cleanupFiles": cleanup_files,
        "cleanupLinksKnown": _cleanup_links_complete(cleanup_files),
        "libraryScanVerified": all(
            bool(private.get("libraryScanVerified"))
            for private in private_records
        ),
        "qbFileListsVerified": all(
            bool(task.get("fileListVerified")) for task in qb_tasks
        ),
        "roots": roots,
        "qbTasks": qb_tasks,
        "moviepilotIndexes": moviepilot_indexes,
        "moviepilotIndexSourceAvailable": all(
            private.get("moviepilotIndexSourceAvailable") is True
            for private in private_records
        ),
    }


def _stable_resource_id(identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"res_{digest}"


def _merge_group(
    items: list[dict[str, Any]],
    identity: str,
) -> dict[str, Any]:
    primary = next(
        (item for item in items if item.get("library")),
        items[0],
    )
    private = _merge_private(items, identity)
    qb_tasks = private["qbTasks"]
    library_items = [item for item in items if item.get("library")]
    title = _clean_text(primary.get("title"), maximum=300)
    english_title = _clean_text(primary.get("englishTitle"), maximum=300)
    identity_conflict = any(
        bool(item.get("_metadataConflict")) for item in items
    )
    for item in items:
        candidate_title = _clean_text(item.get("title"), maximum=300)
        candidate_english = _clean_text(
            item.get("englishTitle"),
            maximum=300,
        )
        if (not has_cjk(title) or title == english_title) and has_cjk(
            candidate_title
        ):
            title = candidate_title
        if (
            not english_title
            or not has_latin(english_title)
            or english_title == title
        ) and candidate_english and has_latin(candidate_english):
            english_title = candidate_english
    hash_name_redacted = bool(
        PUBLIC_HASH_RE.search(title) or PUBLIC_HASH_RE.search(english_title)
    )
    if PUBLIC_HASH_RE.search(title):
        title = "名称待核"
    if PUBLIC_HASH_RE.search(english_title):
        english_title = "Unresolved title"
    if identity_conflict:
        title_aliases = []
        for item in items:
            candidate = _clean_text(item.get("title"), maximum=300)
            if (
                has_cjk(candidate)
                and "待识别" not in candidate
                and candidate not in title_aliases
            ):
                title_aliases.append(candidate)
        if len(title_aliases) > 1:
            title = "（又名：".join(title_aliases[:2]) + "）"
    seasons: set[int] = set()
    for item in items:
        seasons.update(_season_numbers(item.get("edition")))
    for task in qb_tasks:
        seasons.update(_season_numbers(task.get("scope")))
    season_text = _season_label(seasons)
    present_episode_values: set[str] = set()
    for item in items:
        raw_present = item.get("episodePresentEpisodes")
        if not isinstance(raw_present, (list, tuple, set)):
            continue
        present_episode_values.update(
            str(value).strip()
            for value in raw_present
            if str(value).strip()
        )
    episode_values = []
    for item in items:
        value = item.get("episodeActual")
        if value is None:
            continue
        try:
            episode_values.append(int(value))
        except (TypeError, ValueError):
            continue
    episode_actual = (
        len(present_episode_values)
        if present_episode_values
        else max(episode_values) if episode_values else 0
    )
    expected_values = set()
    for item in items:
        value = item.get("episodeExpected")
        if value is None:
            continue
        try:
            expected_values.add(int(value))
        except (TypeError, ValueError):
            continue
    episode_expected = max(expected_values) if expected_values else None
    missing_episode_values: set[str] = set()
    for item in items:
        raw_missing = item.get("episodeMissingEpisodes")
        if not isinstance(raw_missing, (list, tuple, set)):
            continue
        missing_episode_values.update(
            str(value).strip()
            for value in raw_missing
            if str(value).strip()
        )
    # Raw Jellyfin groups can split one season across duplicate identities
    # (for example E01-E09 and E10-E17).  Recompute local gaps from the union
    # and discard stale markers that another merged row actually supplies.
    missing_episode_values.update(_episode_gap_values(present_episode_values))
    missing_episode_values -= present_episode_values
    episode_incomplete = bool(missing_episode_values) or (
        episode_expected is not None
        and 0 < episode_actual < episode_expected
    )
    if (
        len(items) > 1
        and episode_expected is not None
        and episode_actual >= episode_expected
    ):
        # Each raw library row may have been compared with the full expected
        # set before duplicate rows were merged.  Once their union covers the
        # expected count, discard only missing markers that are now present in
        # another row; retain a genuine gap marker such as S01E02.
        missing_episode_values -= present_episode_values
    if episode_expected is not None and episode_actual > episode_expected:
        missing_episode_values = set()
        episode_incomplete = False
    elif not missing_episode_values and episode_expected is not None and episode_actual >= episode_expected:
        episode_incomplete = False
    episode_missing = (
        len(missing_episode_values)
        if missing_episode_values
        else (
            episode_expected - episode_actual
            if episode_incomplete and episode_expected is not None
            else 0
        )
    )
    episode_status = (
        "incomplete"
        if episode_incomplete
        else "complete"
        if episode_expected is not None and episode_actual >= episode_expected
        else ""
    )
    media_kind = (
        "tv"
        if identity.startswith("tv:")
        or any(item.get("type") == "电视剧" for item in items)
        else "movie"
    )
    if media_kind == "tv":
        if (
            len(items) == 1
            and "全季合集" in str(primary.get("edition") or "")
        ):
            edition = str(primary["edition"])
        else:
            edition = season_text or "季数待识别"
            if episode_expected is not None:
                edition += f" · {episode_actual}/{episode_expected} 集"
            elif episode_actual:
                edition += f" · {episode_actual} 集"
            else:
                edition += " · 未入库"
    else:
        edition = "电影" if library_items else "下载区资源 · 未入库"
    inode_sizes: dict[tuple[int, int], int] = {}
    for record in private["files"]:
        try:
            inode_sizes[(int(record["dev"]), int(record["inode"]))] = int(
                record["size"]
            )
        except (KeyError, TypeError, ValueError):
            continue
    size_gib = sum(inode_sizes.values()) / 1024**3
    if not inode_sizes:
        size_gib = sum(float(item.get("size") or 0) for item in items)
    hr = any(bool(item.get("hr")) for item in items)
    hr_pending = any(bool(item.get("hrPending")) for item in items)
    brush = any(bool(item.get("brush")) for item in items)
    name_verified = bilingual_name_verified(
        {
            "title": title,
            "englishTitle": english_title,
            "library": bool(library_items),
            "metadataResolved": all(
                bool(item.get("_metadataResolved")) for item in items
            ),
        }
    ) and not identity_conflict and not hash_name_redacted
    protected = (
        any(bool(item.get("protected")) for item in items)
        or not name_verified
    )
    unfinished = any(_task_is_unfinished(task) for task in qb_tasks)
    task_hr_protected = any(
        bool(task.get("hr")) or bool(task.get("hrUnknown"))
        for task in qb_tasks
    )
    lock_reasons: list[str] = []
    if identity_conflict:
        lock_reasons.append("媒体身份冲突")
    elif not name_verified:
        lock_reasons.append("名称身份待核")
    if unfinished:
        lock_reasons.append("关联任务未完成")
    if hr or hr_pending or task_hr_protected:
        lock_reasons.append("H&R 保护")
    if protected and not lock_reasons:
        lock_reasons.append("安全状态待核")
    lock_reason = "；".join(lock_reasons)
    private["metadataVerified"] = name_verified
    seed_task_groups: dict[
        tuple[str, str, str, str],
        dict[str, Any],
    ] = {}
    for task in qb_tasks:
        status, tone = _task_display_status(task)
        seed_task = {
            "site": str(task.get("site") or "未知站点"),
            "scope": str(task.get("scope") or "整部"),
            "status": status,
            "tone": tone,
        }
        group_key = (
            seed_task["status"],
            seed_task["tone"],
            seed_task["site"],
            seed_task["scope"],
        )
        if group_key not in seed_task_groups:
            seed_task_groups[group_key] = {**seed_task, "count": 0}
        seed_task_groups[group_key]["count"] += 1
    seed_tasks = list(seed_task_groups.values())
    sites = sorted({task["site"] for task in seed_tasks})
    impact_title = (
        "媒体身份冲突，暂不可清理"
        if identity_conflict
        else (
            "名称待核，暂不可清理"
            if not name_verified
            else (
                f"同时影响 {len(qb_tasks)} 个 qB / PT 任务"
                if qb_tasks
                else "不会影响当前做种"
            )
        )
    )
    impact_detail = "、".join(
        (
            f"{task['status']} {task['site']} {task['scope']}"
            + (
                f" ×{task['count']}"
                if int(task.get("count") or 1) > 1
                else ""
            )
        )
        for task in seed_tasks[:4]
    )
    if len(seed_tasks) > 4:
        impact_detail += f" 等 {len(qb_tasks)} 个任务"
    if identity_conflict:
        impact_detail = "同一剧名对应多个媒体身份，已合并展示并锁定清理"
    elif not name_verified:
        impact_detail = "中英文身份无法可靠对应，已锁定全部清理等级"
    elif not qb_tasks:
        impact_detail = "完整删除会让该资源从媒体库消失"
    size_label = _human_size(size_gib)
    library = bool(library_items)
    library_summary = (
        str(primary.get("librarySummary") or "已入库")
        if library
        else "未入库"
    )
    library_detail = (
        str(primary.get("libraryDetail") or "Jellyfin 可播放")
        if library
        else "仅在 qB / 下载区"
    )
    result = {
        **copy.deepcopy(primary),
        "id": _stable_resource_id(identity),
        "title": title,
        "englishTitle": english_title,
        "edition": edition,
        "episodeActual": episode_actual or None,
        "episodePresentEpisodes": sorted(present_episode_values),
        "episodeExpected": episode_expected,
        "episodeMissing": episode_missing or None,
        "episodeMissingEpisodes": sorted(missing_episode_values),
        "episodeIncomplete": episode_incomplete,
        "episodeStatus": episode_status,
        "type": "电视剧" if media_kind == "tv" else "电影",
        "year": next(
            (
                str(item.get("year"))
                for item in items
                if str(item.get("year") or "")
            ),
            "",
        ),
        "size": round(size_gib, 3),
        "sizeLabel": size_label,
        "reclaimLabel": (
            ("完整删除可释放 " if private["allLinksKnown"] else "最多可释放 ")
            + size_label
        ),
        "library": library,
        "hr": hr,
        "hrPending": hr_pending,
        "brush": brush,
        "metadataVerified": name_verified,
        "protected": protected,
        "lockReason": lock_reason,
        "qbSummary": f"{len(qb_tasks)} 个 qB 任务" if qb_tasks else "无 qB 任务",
        "siteSummary": " · ".join(sites) if sites else "媒体库",
        "librarySummary": library_summary,
        "libraryDetail": library_detail,
        "seedTasks": seed_tasks or None,
        "impactTitle": impact_title,
        "impactDetail": impact_detail,
        "qbTasks": f"{len(qb_tasks)} 个关联任务",
        "removeLibrary": (
            "从 Jellyfin 消失；做种任务保持不变"
            if library
            else "未入库，无需处理媒体库"
        ),
        "stopSeeding": (
            f"停止 {len(qb_tasks)} 个关联任务；"
            + ("媒体仍保留" if library else "文件仍保留")
        ),
        "deleteAll": impact_title,
        "_private": private,
    }
    result.pop("_mergeIdentity", None)
    result.pop("_metadataResolved", None)
    result.pop("_metadataConflict", None)
    for legacy_key in (
        "mediaPath",
        "downloadPath",
        "linkCount",
        "qbTasks",
        "removeLibrary",
        "stopSeeding",
        "deleteAll",
    ):
        result.pop(legacy_key, None)
    return result


def _episode_cohort_keys(item: dict[str, Any]) -> list[tuple[str, str]]:
    """Return a conservative display key for duplicate TV library rows.

    Jellyfin can expose one season through multiple provider-less series
    identities (for example a Chinese folder and an English folder).  Keep
    those rows separate for cleanup safety, but allow completeness to use the
    union when their normalized titles/English names agree.  A trailing year
    is presentation noise here; a different known year remains a conflict.
    """

    if _media_kind(item) != "tv" or not item.get("library"):
        return []
    title = _clean_text(item.get("title"), maximum=300)
    english = _clean_text(item.get("englishTitle"), maximum=300)
    title = re.sub(r"[\s([（._-]*(?:19|20)\d{2}[\])）]?$", "", title)
    english = re.sub(
        r"[\s([（._-]*(?:19|20)\d{2}[\])）]?$",
        "",
        english,
    )
    title_key = _release_normalized(title)
    english_key = _release_normalized(english)
    keys: list[tuple[str, str]] = []
    if english_key:
        keys.append(("english", english_key))
    if title_key:
        keys.append(("title", title_key))
    return list(dict.fromkeys(keys))


def _overlay_tv_episode_cohorts(resources: list[dict[str, Any]]) -> None:
    """Reconcile completeness across duplicate library display rows.

    This intentionally does not merge private deletion records.  It only
    prevents a split series from reporting each half as incomplete when the
    union is complete, while preserving the existing identity-conflict lock.
    """

    cohorts: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in resources:
        if not item.get("episodePresentEpisodes"):
            continue
        keys = _episode_cohort_keys(item)
        for key in keys:
            cohorts[key].append(item)
    for items in cohorts.values():
        if len(items) < 2:
            continue
        years = {
            str(item.get("year") or "")
            for item in items
            if str(item.get("year") or "")
        }
        if len(years) > 1:
            continue
        expected_values = set()
        for item in items:
            value = item.get("episodeExpected")
            if value is None:
                continue
            try:
                expected_values.add(int(value))
            except (TypeError, ValueError):
                continue
        if len(expected_values) > 1:
            continue
        present = {
            str(value).strip()
            for item in items
            for value in item.get("episodePresentEpisodes") or []
            if str(value).strip()
        }
        if not present:
            continue
        missing = {
            str(value).strip()
            for item in items
            for value in item.get("episodeMissingEpisodes") or []
            if str(value).strip()
        }
        missing.update(_episode_gap_values(present))
        missing -= present
        expected = next(iter(expected_values), None)
        actual = len(present)
        incomplete = bool(missing) or (
            expected is not None and 0 < actual < expected
        )
        episode_missing = len(missing) if missing else (
            expected - actual
            if incomplete and expected is not None
            else 0
        )
        seasons = _season_numbers(" ".join(sorted(present)))
        season_text = _season_label(seasons)
        for item in items:
            item["episodeActual"] = actual
            item["episodePresentEpisodes"] = sorted(present)
            item["episodeExpected"] = expected
            item["episodeMissingEpisodes"] = sorted(missing)
            item["episodeMissing"] = episode_missing or None
            item["episodeIncomplete"] = incomplete
            item["episodeStatus"] = (
                "incomplete"
                if incomplete
                else "complete"
                if expected is not None and actual >= expected
                else ""
            )
            if season_text and "全季合集" not in str(item.get("edition") or ""):
                item["edition"] = (
                    f"{season_text} · {actual}/{expected} 集"
                    if expected is not None
                    else f"{season_text} · {actual} 集"
                )
                if item.get("libraryDetail"):
                    item["libraryDetail"] = (
                        f"Jellyfin 可播放 · {actual}/{expected} 集"
                        if expected is not None
                        else f"Jellyfin 可播放 · {actual} 集"
                    )


def _overlay_shared_hardlink_resources(resources: list[dict[str, Any]]) -> None:
    """Expose safe cross-resource hard-link summaries for the public snapshot.

    Cleanup planning already accounts for shared ``(dev, inode)`` ownership.
    The resource cards historically only saw each row's own qB tasks, which
    made a media-only row appear to have no seeding impact even when its files
    were hard-linked to another row's qB payload.  Publish titles and safety
    state for related rows, but never paths, hashes, or inode details.
    """

    inode_owners: dict[tuple[int, int], set[str]] = defaultdict(set)
    resource_inodes: dict[str, set[tuple[int, int]]] = {}
    by_id: dict[str, dict[str, Any]] = {}
    for item in resources:
        resource_id = str(item.get("id") or "")
        if not resource_id:
            continue
        by_id[resource_id] = item
        inodes: set[tuple[int, int]] = set()
        for record in (item.get("_private") or {}).get("cleanupFiles") or []:
            if not record.get("exists"):
                continue
            try:
                dev = int(record.get("dev") or 0)
                inode = int(record.get("inode") or 0)
            except (TypeError, ValueError):
                continue
            if dev > 0 and inode > 0:
                inodes.add((dev, inode))
        resource_inodes[resource_id] = inodes
        for inode in inodes:
            inode_owners[inode].add(resource_id)

    for item in resources:
        resource_id = str(item.get("id") or "")
        related_ids = sorted(
            {
                owner_id
                for inode in resource_inodes.get(resource_id, set())
                for owner_id in inode_owners.get(inode, set())
                if owner_id != resource_id
            }
        )
        item["sharedHardlinkResources"] = [
            {
                "id": owner_id,
                "title": str(related.get("title") or owner_id),
                "englishTitle": str(related.get("englishTitle") or ""),
                "edition": str(related.get("edition") or ""),
                "protected": bool(related.get("protected")),
                "metadataVerified": related.get("metadataVerified") is True,
            }
            for owner_id in related_ids
            for related in [by_id.get(owner_id) or {}]
        ]


def enrich_and_merge_resources(
    resources: list[dict[str, Any]],
    cache: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    sanitized = sanitize_metadata_cache(cache)
    entries = sanitized["entries"]
    exact_aliases: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    exact_aliases_without_year: dict[
        tuple[str, str],
        set[str],
    ] = defaultdict(set)
    exact_title_aliases: dict[
        tuple[str, str, str],
        set[str],
    ] = defaultdict(set)
    exact_title_aliases_without_year: dict[
        tuple[str, str],
        set[str],
    ] = defaultdict(set)
    for raw_item in resources:
        identity = str((raw_item.get("_private") or {}).get("identity") or "")
        english = _clean_text(raw_item.get("englishTitle"), maximum=300)
        title = _clean_text(raw_item.get("title"), maximum=300)
        year = _clean_text(raw_item.get("year"), maximum=4)
        if (
            re.fullmatch(r"(?:movie|tv):tmdb:\d+", identity)
            and has_latin(english)
        ):
            exact_aliases[
                (_media_kind(raw_item), english.casefold(), year)
            ].add(identity)
            exact_aliases_without_year[
                (_media_kind(raw_item), english.casefold())
            ].add(identity)
        if (
            re.fullmatch(r"(?:movie|tv):tmdb:\d+", identity)
            and has_cjk(title)
            and "待识别" not in title
        ):
            exact_title_aliases[
                (_media_kind(raw_item), title, year)
            ].add(identity)
            exact_title_aliases_without_year[
                (_media_kind(raw_item), title)
            ].add(identity)
    for entry in entries.values():
        identity = str(entry.get("identity") or "")
        english = _clean_text(entry.get("englishTitle"), maximum=300)
        title = _clean_text(entry.get("title"), maximum=300)
        year = _clean_text(entry.get("year"), maximum=4)
        if (
            entry.get("status") == "resolved"
            and re.fullmatch(r"(?:movie|tv):tmdb:\d+", identity)
            and has_latin(english)
        ):
            exact_aliases[
                (str(entry["kind"]), english.casefold(), year)
            ].add(identity)
            exact_aliases_without_year[
                (str(entry["kind"]), english.casefold())
            ].add(identity)
        if (
            entry.get("status") == "resolved"
            and re.fullmatch(r"(?:movie|tv):tmdb:\d+", identity)
            and has_cjk(title)
            and "待识别" not in title
        ):
            exact_title_aliases[
                (str(entry["kind"]), title, year)
            ].add(identity)
            exact_title_aliases_without_year[
                (str(entry["kind"]), title)
            ].add(identity)
    prepared: list[dict[str, Any]] = []
    resolved_qb = 0
    unresolved_qb = 0
    resolved_all = 0
    for raw_item in resources:
        item = copy.deepcopy(raw_item)
        private = item.get("_private") or {}
        identity = str(private.get("identity") or "")
        entry = entries.get(metadata_cache_key(item))
        if (
            entry
            and entry.get("status") == "resolved"
            and entry.get("query") == _metadata_query(item)
            and entry.get("kind") == _media_kind(item)
        ):
            item["title"] = entry["title"]
            item["englishTitle"] = entry["englishTitle"]
            if entry.get("year"):
                item["year"] = entry["year"]
            item["type"] = (
                "电视剧" if entry["kind"] == "tv" else "电影"
            )
            direct_aliases = exact_aliases.get(
                (
                    entry["kind"],
                    str(entry["englishTitle"]).casefold(),
                    str(entry.get("year") or ""),
                ),
                set(),
            )
            if not entry.get("year"):
                direct_aliases = exact_aliases_without_year.get(
                    (
                        entry["kind"],
                        str(entry["englishTitle"]).casefold(),
                    ),
                    set(),
                )
            title_aliases = exact_title_aliases.get(
                (
                    entry["kind"],
                    str(entry["title"]),
                    str(entry.get("year") or ""),
                ),
                set(),
            )
            if not entry.get("year"):
                title_aliases = exact_title_aliases_without_year.get(
                    (entry["kind"], str(entry["title"])),
                    set(),
                )
            direct_aliases = set(direct_aliases) | set(title_aliases)
            item["_mergeIdentity"] = (
                next(iter(direct_aliases))
                if len(direct_aliases) == 1
                else (
                    entry["identity"]
                    if str(entry["identity"]).startswith(
                        f"{entry['kind']}:tmdb:"
                    )
                    or not re.fullmatch(
                        r"(?:movie|tv):(?:tmdb|tvdb|imdb):[^:]+",
                        identity,
                    )
                    else identity
                )
            )
            item["_metadataResolved"] = True
            resolved_all += 1
            if not item.get("library"):
                resolved_qb += 1
        else:
            parsed_english = _clean_text(
                (entry or {}).get("parsedEnglish"),
                maximum=300,
            )
            parsed_year = _clean_text(
                (entry or {}).get("parsedYear"),
                maximum=4,
            )
            alias_identities = exact_aliases.get(
                (
                    _media_kind(item),
                    parsed_english.casefold(),
                    parsed_year,
                ),
                set(),
            )
            direct_english = _clean_text(
                item.get("englishTitle"),
                maximum=300,
            )
            direct_year = _clean_text(item.get("year"), maximum=4)
            direct_alias_identities = exact_aliases.get(
                (
                    _media_kind(item),
                    direct_english.casefold(),
                    direct_year,
                ),
                set(),
            )
            if not direct_year:
                direct_alias_identities = exact_aliases_without_year.get(
                    (_media_kind(item), direct_english.casefold()),
                    set(),
                )
            direct_title = _clean_text(item.get("title"), maximum=300)
            title_alias_identities = exact_title_aliases.get(
                (
                    _media_kind(item),
                    direct_title,
                    direct_year,
                ),
                set(),
            )
            if not direct_year:
                title_alias_identities = (
                    exact_title_aliases_without_year.get(
                        (_media_kind(item), direct_title),
                        set(),
                    )
                )
            direct_alias_identities = (
                set(direct_alias_identities) | set(title_alias_identities)
            )
            item["_mergeIdentity"] = (
                identity
                if item.get("library")
                and re.fullmatch(
                    r"(?:movie|tv):(?:tmdb|tvdb|imdb):[^:]+",
                    identity,
                )
                else (
                    next(iter(direct_alias_identities))
                    if len(direct_alias_identities) == 1
                    else (
                        next(iter(alias_identities))
                        if len(alias_identities) == 1
                        else f"resource:{item['id']}"
                    )
                )
            )
            if not item.get("library"):
                unresolved_qb += 1
        prepared.append(item)
    title_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(
        list
    )
    for item in prepared:
        title = _clean_text(item.get("title"), maximum=300)
        if has_cjk(title) and "待识别" not in title:
            title_groups[(_media_kind(item), title)].append(item)
    for (kind, title_key), title_items in title_groups.items():
        if len(title_items) < 2:
            continue
        years = {
            str(item.get("year") or "")
            for item in title_items
            if str(item.get("year") or "")
        }
        exact_identities = {
            str(item.get("_mergeIdentity") or "")
            for item in title_items
            if re.fullmatch(
                rf"{kind}:tmdb:\d+",
                str(item.get("_mergeIdentity") or ""),
            )
        }
        if len(years) > 1 or len(exact_identities) < 2:
            continue
        conflict_identity = (
            f"{kind}:conflict:"
            + hashlib.sha256(title_key.encode("utf-8")).hexdigest()[:24]
        )
        for item in title_items:
            item["_mergeIdentity"] = conflict_identity
            item["_metadataConflict"] = True

    display_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(
        list
    )
    for item in prepared:
        english = _clean_text(item.get("englishTitle"), maximum=300)
        if has_latin(english):
            display_groups[
                (_media_kind(item), english.casefold())
            ].append(item)
    for (kind, english_key), display_items in display_groups.items():
        if len(display_items) < 2:
            continue
        years = {
            str(item.get("year") or "")
            for item in display_items
            if str(item.get("year") or "")
        }
        exact_identities = {
            str(item.get("_mergeIdentity") or "")
            for item in display_items
            if re.fullmatch(
                rf"{kind}:tmdb:\d+",
                str(item.get("_mergeIdentity") or ""),
            )
        }
        if len(years) > 1 or len(exact_identities) < 2:
            continue
        conflict_identity = (
            f"{kind}:conflict:"
            + hashlib.sha256(english_key.encode("utf-8")).hexdigest()[:24]
        )
        for item in display_items:
            item["_mergeIdentity"] = conflict_identity
            item["_metadataConflict"] = True
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    order: list[str] = []
    for item in prepared:
        identity = str(item.get("_mergeIdentity") or f"resource:{item['id']}")
        if identity not in groups:
            order.append(identity)
        groups[identity].append(item)
    merged = [_merge_group(groups[identity], identity) for identity in order]
    _overlay_tv_episode_cohorts(merged)
    _overlay_shared_hardlink_resources(merged)
    merged.sort(key=lambda item: (-float(item.get("size") or 0), item["title"]))
    return merged, {
        "metadataResolvedResources": resolved_all,
        "metadataResolvedQbResources": resolved_qb,
        "metadataUnresolvedQbResources": unresolved_qb,
        "bilingualMissingResources": sum(
            1 for item in merged if needs_bilingual_name(item)
        ),
        "metadataUnverifiedResources": sum(
            1 for item in merged if item.get("metadataVerified") is not True
        ),
    }
