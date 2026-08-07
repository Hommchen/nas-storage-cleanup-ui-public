#!/opt/homebrew/bin/python3.12
"""Build deterministic, non-mutating cleanup plans from a private inventory."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import PurePosixPath, Path
from typing import Any


PLAN_VERSION = 1
VALID_MODES = {"pause", "retire", "delete"}
MODE_LABELS = {
    "pause": "停止做种",
    "retire": "退出做种",
    "delete": "完整删除",
}
ALLOWED_ROOTS = tuple(
    PurePosixPath(path)
    for path in (
        "/mnt/sdc/downloads/completed",
        "/mnt/sdd/downloads/completed",
        "/mnt/sdc/.media-main/Movies",
        "/mnt/sdd/media/TV",
        "/mnt/sdc/media/Movies",
        "/mnt/sdc/media/TV",
        "/mnt/sdc/.media-quarantine",
        "/mnt/sdd/.media-quarantine",
    )
)


class PlanInputError(ValueError):
    """Raised when a plan request itself is malformed."""


def path_is_allowed(
    value: str,
    *,
    allowed_roots: tuple[PurePosixPath, ...] | list[str] | None = None,
) -> bool:
    if not value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        return False
    roots = tuple(
        PurePosixPath(item) for item in (allowed_roots or ALLOWED_ROOTS)
    )
    return any(path == root or root in path.parents for root in roots)


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _add_reason(target: list[dict[str, str]], code: str, message: str) -> None:
    if not any(item["code"] == code for item in target):
        target.append({"code": code, "message": message})


def _inode_accounting(
    resource: dict[str, Any],
    field: str = "cleanupFiles",
) -> tuple[bool, int]:
    paths_by_inode: dict[tuple[int, int], set[str]] = defaultdict(set)
    expected_links: dict[tuple[int, int], int] = {}
    unique_sizes: dict[tuple[int, int], int] = {}
    for item in resource.get(field) or []:
        if not item.get("exists"):
            continue
        key = (int(item.get("dev") or 0), int(item.get("inode") or 0))
        paths_by_inode[key].add(str(item.get("path") or ""))
        expected_links[key] = int(item.get("nlink") or 0)
        unique_sizes[key] = int(item.get("size") or 0)
    complete = bool(paths_by_inode) and all(
        len(paths) == expected_links[key] and expected_links[key] > 0
        for key, paths in paths_by_inode.items()
    )
    return complete, sum(unique_sizes.values())


def _resource_inodes(resource: dict[str, Any]) -> set[tuple[int, int]]:
    """Return the existing hard-link identities owned by a resource."""

    result: set[tuple[int, int]] = set()
    for item in resource.get("cleanupFiles") or []:
        if not item.get("exists"):
            continue
        try:
            dev = int(item.get("dev") or 0)
            inode = int(item.get("inode") or 0)
        except (TypeError, ValueError):
            continue
        if dev > 0 and inode > 0:
            result.add((dev, inode))
    return result


def build_plan(
    inventory: dict[str, Any],
    *,
    snapshot_id: str,
    resource_ids: list[str],
    mode: str,
    acknowledge_site_risk: bool = False,
    allowed_roots: tuple[PurePosixPath, ...] | list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a deterministic plan. This function never performs mutations."""

    if mode not in VALID_MODES:
        raise PlanInputError(f"unsupported mode: {mode}")
    if not snapshot_id:
        raise PlanInputError("snapshotId is required")
    if not resource_ids:
        raise PlanInputError("at least one resourceId is required")
    if len(resource_ids) > 100:
        raise PlanInputError("at most 100 resources may be planned at once")
    if len(resource_ids) != len(set(resource_ids)):
        raise PlanInputError("duplicate resourceId values are not allowed")
    if inventory.get("schemaVersion") != 2:
        raise PlanInputError("unsupported inventory schema")

    created_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    blocks: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    if snapshot_id != inventory.get("snapshotId"):
        _add_reason(
            blocks,
            "stale_snapshot",
            "页面数据已经变化，请刷新后重新选择。",
        )
    inventory_stats = inventory.get("stats") or {}
    if int(inventory_stats.get("unresolvedTransactions") or 0) > 0:
        _add_reason(
            blocks,
            "unresolved_transaction",
            "存在未完成的清理事务，已锁定全部操作，请先恢复或核对。",
        )
    # New snapshots carry per-site H&R state.  Keep the legacy global block
    # only for old inventories without that detail; with per-site state we can
    # still plan an unrelated public-BT resource while protecting affected
    # private-site tasks below.
    has_hr_sources = isinstance(inventory_stats.get("hrSources"), dict)
    hr_sources = inventory_stats.get("hrSources") or {}
    failed_hr_sites = {
        str(source.get("taskLabel") or site)
        for site, source in hr_sources.items()
        if isinstance(source, dict)
        and (
            source.get("validated") is True
            or "validated" not in source
        )
        and source.get("available") is not True
    }
    if (
        mode == "delete"
        and not has_hr_sources
        and not inventory_stats.get("hrSourceAvailable", False)
    ):
        _add_reason(
            blocks,
            "hr_source_unavailable",
            "无法核实学校站 H&R，禁止完整删除。",
        )
    missing_unassigned = inventory_stats.get("hrMissingUnassigned")
    if missing_unassigned is None:
        missing_unassigned = inventory_stats.get("hrMissingUncovered") or 0
    if (
        mode == "delete"
        and inventory_stats.get("hrEnabled") is not False
        and (
            "hrEffectiveSites" not in inventory_stats
            or int(inventory_stats.get("hrEffectiveSites") or 0) > 0
        )
        and int(missing_unassigned) > 0
    ):
        _add_reason(
            warnings,
            "unassigned_hr_recovery",
            "仍有 H&R 缺口无法精确关联媒体；当前计划只按所选资源自身保护状态执行。",
        )

    resources_by_id = inventory.get("resources") or {}
    selected_id_set = set(resource_ids)
    inode_owners: dict[tuple[int, int], set[str]] = defaultdict(set)
    resource_inodes: dict[str, set[tuple[int, int]]] = {}
    for owner_id, owner in resources_by_id.items():
        if not isinstance(owner, dict):
            continue
        inodes = _resource_inodes(owner)
        resource_inodes[str(owner_id)] = inodes
        for inode in inodes:
            inode_owners[inode].add(str(owner_id))
    selected_resources: list[dict[str, Any]] = []
    qb_tasks_by_hash: dict[str, dict[str, Any]] = {}
    files_by_path: dict[str, dict[str, Any]] = {}
    moviepilot_indexes_by_id: dict[int, dict[str, Any]] = {}
    reclaim_inodes: dict[tuple[int, int], int] = {}
    requires_site_ack = False

    for resource_id in resource_ids:
        resource = resources_by_id.get(resource_id)
        if not resource:
            _add_reason(
                blocks,
                "resource_missing",
                f"资源 {resource_id} 已不存在，请刷新后重试。",
            )
            continue

        resource_blocks: list[dict[str, str]] = []
        resource_warnings: list[dict[str, str]] = []
        shared_resource_ids = (
            sorted(
                {
                    owner_id
                    for inode in resource_inodes.get(resource_id, set())
                    for owner_id in inode_owners.get(inode, set())
                    if owner_id != resource_id and owner_id not in selected_id_set
                }
            )
            if mode == "delete"
            else []
        )
        tasks = resource.get("qbTasks") or []
        moviepilot_indexes = resource.get("moviepilotIndexes") or []
        if (
            mode == "delete"
            and has_hr_sources
            and any(
                task.get("private")
                and str(task.get("site") or "") in failed_hr_sites
                for task in tasks
            )
        ):
            _add_reason(
                resource_blocks,
                "hr_source_unavailable",
                "无法核实受影响私有站的 H&R，禁止完整删除该资源。",
            )
        if resource.get("metadataVerified") is not True:
            _add_reason(
                resource_blocks,
                "metadata_unverified",
                "中英文名称尚未可靠对应，已锁定全部清理等级。",
            )
        if resource.get("protected"):
            _add_reason(
                resource_blocks,
                "protected_resource",
                "资源仍在 H&R、下载或待核保护中。",
            )
        if any(task.get("progress", 0) < 1 for task in tasks):
            _add_reason(
                resource_blocks,
                "unfinished_task",
                "关联下载尚未完成。",
            )
        if any(task.get("hr") or task.get("hrUnknown") for task in tasks):
            _add_reason(
                resource_blocks,
                "hr_protected",
                "关联任务仍受 H&R 保护或状态待核。",
            )

        if mode == "delete" and resource.get("library"):
            if resource.get("moviepilotIndexSourceAvailable") is not True:
                _add_reason(
                    resource_blocks,
                    "moviepilot_index_source_unavailable",
                    "无法核实 MoviePilot 媒体库索引，禁止完整删除。",
                )
            if not isinstance(moviepilot_indexes, list):
                _add_reason(
                    resource_blocks,
                    "moviepilot_index_invalid",
                    "MoviePilot 媒体库索引清单格式不可信，禁止完整删除。",
                )
            else:
                for index in moviepilot_indexes:
                    if not isinstance(index, dict):
                        _add_reason(
                            resource_blocks,
                            "moviepilot_index_invalid",
                            "MoviePilot 媒体库索引清单格式不可信，禁止完整删除。",
                        )
                        continue
                    try:
                        index_id = int(index.get("id"))
                    except (TypeError, ValueError):
                        index_id = 0
                    if index_id <= 0 or not str(index.get("path") or ""):
                        _add_reason(
                            resource_blocks,
                            "moviepilot_index_invalid",
                            "MoviePilot 媒体库索引缺少可靠身份，禁止完整删除。",
                        )
                        continue
                    moviepilot_indexes_by_id.setdefault(index_id, index)

        if mode == "delete" and shared_resource_ids:
            shared_labels = []
            for shared_id in shared_resource_ids[:4]:
                shared = resources_by_id.get(shared_id) or {}
                label = str(shared.get("title") or shared_id)
                edition = str(shared.get("edition") or "").strip()
                shared_labels.append(f"{label}（{edition}）" if edition else label)
            label_text = "、".join(shared_labels)
            if len(shared_resource_ids) > len(shared_labels):
                label_text += f"等 {len(shared_resource_ids)} 项"
            _add_reason(
                resource_blocks,
                "shared_hardlink_resource",
                "该资源与未选中的其他资源"
                + (f"（{label_text}）" if label_text else "")
                + "共享硬链接，不能单独完整删除；请同时选择关联资源后重新生成计划。",
            )

        private_tasks = [task for task in tasks if task.get("private")]
        if private_tasks:
            requires_site_ack = True
            _add_reason(
                resource_warnings,
                "private_tracker_impact",
                "将影响私有站做种；当前计划不代表站点最低保种规则已满足。",
            )
        if any(task.get("selfPublish") for task in tasks):
            _add_reason(
                resource_warnings,
                "self_publish_impact",
                "包含自发布任务，退出后可能失去唯一做种来源。",
            )

        if mode in {"pause", "retire"} and not tasks:
            _add_reason(
                resource_warnings,
                "no_qb_task",
                "该资源没有关联 qB 任务，本档操作不会改变它。",
            )

        resource_reclaim = 0
        if mode == "delete":
            accounting_complete, resource_reclaim = _inode_accounting(resource)
            if (
                not resource.get("allLinksKnown")
                or not resource.get("cleanupLinksKnown")
                or not accounting_complete
            ):
                _add_reason(
                    resource_blocks,
                    "unknown_hardlinks",
                    "仍有未定位硬链接，无法证明完整删除会安全释放空间。",
                )
            cleanup_files = resource.get("cleanupFiles") or []
            existing_files = [
                item for item in cleanup_files if item.get("exists")
            ]
            if tasks and not resource.get("qbFileListsVerified"):
                _add_reason(
                    resource_blocks,
                    "unverified_qb_file_list",
                    "无法从 qB 复核逐文件清单，禁止按目录猜测删除。",
                )
            if not resource.get("libraryScanVerified"):
                _add_reason(
                    resource_blocks,
                    "unverified_library_scan",
                    "媒体库目录未能完整扫描，禁止继续删除。",
                )
            if any(
                item.get("required") and not item.get("exists")
                for item in cleanup_files
            ):
                _add_reason(
                    resource_blocks,
                    "required_file_missing",
                    "qB 或媒体库声明的完整文件已经缺失，请先刷新并核对。",
                )
            if any(
                item.get("exists") and not item.get("regular")
                for item in cleanup_files
            ):
                _add_reason(
                    resource_blocks,
                    "non_regular_file",
                    "清理清单包含符号链接或非普通文件，已拒绝执行。",
                )
            if any(not item.get("relativeSafe") for item in cleanup_files):
                _add_reason(
                    resource_blocks,
                    "unsafe_qb_relative_path",
                    "qB 文件清单含绝对路径或上级跳转，已拒绝执行。",
                )
            if not existing_files:
                _add_reason(
                    resource_blocks,
                    "no_verified_files",
                    "没有可复核的实际文件，禁止完整删除。",
                )
            if any(
                (
                    item.get("allowed")
                    and not path_is_allowed(
                        str(item.get("path") or ""),
                        allowed_roots=allowed_roots,
                    )
                )
                or (not item.get("allowed") and not item.get("legacyQuarantine"))
                for item in existing_files
            ):
                _add_reason(
                    resource_blocks,
                    "path_outside_allowlist",
                    "至少一个文件不在允许清理的媒体或下载目录内。",
                )
            if any(
                "/.media-quarantine/" in str(item.get("path") or "")
                for item in existing_files
            ):
                _add_reason(
                    resource_warnings,
                    "legacy_quarantine_impact",
                    "完整删除将同时清除历史媒体隔离区中的同一硬链接。",
                )

        for task in tasks:
            task_hash = str(task.get("hash") or "")
            if task_hash:
                qb_tasks_by_hash.setdefault(task_hash, task)
        if mode == "delete" and not resource_blocks:
            for item in resource.get("cleanupFiles") or []:
                if not item.get("exists"):
                    continue
                files_by_path.setdefault(str(item["path"]), item)
                inode_key = (
                    int(item.get("dev") or 0),
                    int(item.get("inode") or 0),
                )
                reclaim_inodes.setdefault(inode_key, int(item.get("size") or 0))

        selected_resources.append(
            {
                "id": resource_id,
                "title": resource.get("title") or "",
                "englishTitle": resource.get("englishTitle") or "",
                "edition": resource.get("edition") or "",
                "sizeGiB": resource.get("sizeGiB") or 0,
                "taskCount": len(tasks),
                "fileCount": len(
                    [
                        item
                        for item in resource.get("cleanupFiles") or []
                        if item.get("exists")
                    ]
                ),
                "moviepilotIndexCount": len(moviepilot_indexes),
                "sharedResourceIds": shared_resource_ids,
                "blocked": bool(resource_blocks),
                "blocks": resource_blocks,
                "warnings": resource_warnings,
            }
        )
        for reason in resource_blocks:
            _add_reason(blocks, reason["code"], reason["message"])
        for warning in resource_warnings:
            _add_reason(warnings, warning["code"], warning["message"])

    if requires_site_ack and not acknowledge_site_risk:
        _add_reason(
            blocks,
            "site_risk_not_acknowledged",
            "需要明确确认私有站做种与保种风险。",
        )

    if mode in {"pause", "retire"} and not qb_tasks_by_hash:
        _add_reason(
            blocks,
            "nothing_to_do",
            "所选资源没有可执行的 qB 任务。",
        )
    if mode == "delete" and not files_by_path and not blocks:
        _add_reason(
            blocks,
            "nothing_to_do",
            "所选资源没有可执行的文件清理动作。",
        )

    plan_material = {
        "mode": mode,
        "resourceIds": sorted(resource_ids),
        "acknowledgeSiteRisk": acknowledge_site_risk,
        "qbTaskHashes": sorted(qb_tasks_by_hash),
        "moviepilotIndexIds": sorted(moviepilot_indexes_by_id),
        "sharedResourceIds": sorted(
            {
                owner_id
                for item in selected_resources
                for owner_id in item.get("sharedResourceIds") or []
            }
        ),
        "fileStates": sorted(
            (
                path,
                item.get("dev"),
                item.get("inode"),
                item.get("size"),
                item.get("nlink"),
            )
            for path, item in files_by_path.items()
        ),
    }
    plan_id = "plan_" + _canonical_digest(plan_material)[:24]
    confirm_phrase = f"{MODE_LABELS[mode]} {len(selected_resources)} 项"

    return {
        "planVersion": PLAN_VERSION,
        "planId": plan_id,
        "snapshotId": snapshot_id,
        "createdAt": created_at.isoformat(timespec="seconds"),
        "expiresAt": (created_at + timedelta(minutes=5)).isoformat(timespec="seconds"),
        "mode": mode,
        "modeLabel": MODE_LABELS[mode],
        "confirmPhrase": confirm_phrase,
        "canExecute": not blocks,
        "requiresSiteAcknowledgement": requires_site_ack,
        "acknowledgeSiteRisk": acknowledge_site_risk,
        "estimatedReclaimBytes": sum(reclaim_inodes.values())
        if mode == "delete"
        else 0,
        "resources": selected_resources,
        "blocks": blocks,
        "warnings": warnings,
        "fileExpectations": {
            path: {
                key: item.get(key)
                for key in ("dev", "inode", "size", "nlink")
            }
            for path, item in sorted(files_by_path.items())
        },
        "operations": {
            "qbStop": sorted(qb_tasks_by_hash) if mode == "pause" else [],
            "qbRemoveKeepFiles": sorted(qb_tasks_by_hash)
            if mode in {"retire", "delete"}
            else [],
            "unlinkFiles": sorted(files_by_path) if mode == "delete" else [],
            "moviepilotIndexes": (
                [
                    moviepilot_indexes_by_id[index_id]
                    for index_id in sorted(moviepilot_indexes_by_id)
                ]
                if mode == "delete"
                else []
            ),
        },
    }


def public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Strip qB hashes and absolute paths before returning a plan to the UI."""

    result = {
        key: value
        for key, value in plan.items()
        if key not in {"operations", "fileExpectations"}
    }
    result["operationCounts"] = {
        "qbStop": len(plan["operations"]["qbStop"]),
        "qbRemoveKeepFiles": len(plan["operations"]["qbRemoveKeepFiles"]),
        "unlinkFiles": len(plan["operations"]["unlinkFiles"]),
        "moviepilotIndexes": len(plan["operations"]["moviepilotIndexes"]),
    }
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".runtime/resource-inventory.json",
    )
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--mode", choices=sorted(VALID_MODES), required=True)
    parser.add_argument("--resource-id", action="append", dest="resource_ids", required=True)
    parser.add_argument("--acknowledge-site-risk", action="store_true")
    parser.add_argument("--public", action="store_true", help="Strip hashes and paths.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    with args.inventory.open(encoding="utf-8") as handle:
        inventory = json.load(handle)
    try:
        plan = build_plan(
            inventory,
            snapshot_id=args.snapshot_id,
            resource_ids=args.resource_ids,
            mode=args.mode,
            acknowledge_site_risk=args.acknowledge_site_risk,
        )
    except PlanInputError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            public_plan(plan) if args.public else plan,
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if plan["canExecute"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
