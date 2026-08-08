"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type LegacyFilter =
  | "all"
  | "movie"
  | "tv"
  | "tv-incomplete"
  | "library"
  | "hr"
  | "review"
  | "names";
type FilterGroupId = "type" | "library" | "seed" | "flags";
type FilterState = {
  type: "all" | "movie" | "tv";
  library: "all" | "imported" | "not-imported";
  seed: "all" | "hr" | "none";
  flags: string[];
};
type ActionMode = "pause" | "retire" | "delete";
type ControlStatus = "connecting" | "ready" | "offline";

type SeedTask = {
  site: string;
  scope: string;
  status: string;
  tone: "normal" | "warning" | "protected";
  count?: number;
};

type SharedHardlinkResource = {
  id: string;
  title: string;
  englishTitle: string;
  edition: string;
  protected: boolean;
  metadataVerified?: boolean;
};

type Resource = {
  id: string;
  title: string;
  englishTitle: string;
  edition: string;
  type: "电影" | "电视剧";
  year: string;
  size: number;
  sizeLabel: string;
  reclaimLabel: string;
  library: boolean;
  episodeStatus?: string;
  episodeIncomplete?: boolean;
  episodeActual?: number | null;
  episodeExpected?: number | null;
  episodeMissing?: number | null;
  episodeMissingEpisodes?: string[] | null;
  hr: boolean;
  hrPending?: boolean;
  brush: boolean;
  metadataVerified?: boolean;
  protected: boolean;
  qbSummary: string;
  siteSummary: string;
  librarySummary: string;
  libraryDetail: string;
  seedTasks?: SeedTask[] | null;
  impactTitle: string;
  impactDetail: string;
  lockReason?: string;
  sharedHardlinkResources?: SharedHardlinkResource[];
};

type Snapshot = {
  schemaVersion: 2;
  snapshotId: string;
  generatedAt: string;
  source: string;
  stats: {
    resources: number;
    jellyfinGroups: number;
    qbTasks: number;
    qbFileListsCached?: number;
    matchedQbTasks: number;
    unmatchedQbTasks: number;
    hrSourceAvailable: boolean;
    hrActiveTitles: number;
    hrMatchedQbTasks: number;
    hrMissingQbTasks?: number;
    hrMissingUncovered?: number;
    hrMissingUnassigned?: number;
    hrRecoveryCandidates?: number;
    hrMissingLinkedRecords?: number;
    hrMissingLinkedResources?: number;
    unresolvedTransactions?: number;
    metadataResolvedResources?: number;
    metadataResolvedQbResources?: number;
    metadataUnresolvedQbResources?: number;
    bilingualMissingResources?: number;
    metadataUnverifiedResources?: number;
    metadataSourceAvailable?: boolean;
    metadataManualOverrides?: number;
  };
  resources: Resource[];
};

type PlanIssue = {
  code: string;
  message: string;
};

type MissingFileDetail = {
  source: string;
  name: string;
  episode?: string;
  expectedSizeBytes?: number;
};

type PlannedResource = {
  id: string;
  title: string;
  englishTitle: string;
  edition: string;
  sizeGiB: number;
  taskCount: number;
  fileCount: number;
  blocked: boolean;
  blocks: PlanIssue[];
  warnings: PlanIssue[];
  missingFiles?: MissingFileDetail[];
};

type PublicPlan = {
  planVersion: number;
  planId: string;
  snapshotId: string;
  createdAt: string;
  expiresAt: string;
  mode: ActionMode;
  modeLabel: string;
  confirmPhrase: string;
  canExecute: boolean;
  requiresSiteAcknowledgement: boolean;
  acknowledgeSiteRisk: boolean;
  requiresMissingFileAcknowledgement: boolean;
  acknowledgeMissingFiles: boolean;
  estimatedReclaimBytes: number;
  resources: PlannedResource[];
  blocks: PlanIssue[];
  warnings: PlanIssue[];
  operationCounts: {
    qbStop: number;
    qbRemoveKeepFiles: number;
    unlinkFiles: number;
    moviepilotIndexes: number;
  };
};

type ExecutionResult = {
  planId: string;
  mode: ActionMode;
  qbStopped: number;
  qbRemoved: number;
  filesDeleted: number;
  missingFilesAlreadyAbsent?: number;
  moviepilotIndexesDeleted: number;
  backupCreated: boolean;
  snapshotRefreshPending: boolean;
};

type RecoveryStatus = {
  planId: string;
  phase: string;
  mode: "retire" | "delete";
  taskCount: number;
  tasksPresent: number;
  tasksAbsent: number;
  filesAtSource: number;
  filesQuarantined: number;
  filesAlreadyGone: number;
  rollbackPhrase: string;
  finalizePhrase: string;
};

type RecoveryAction = "rollback" | "finalize";

type ProtectionGap = {
  title: string;
  coveredByCandidate: boolean;
  qbTaskPresent: boolean;
  linkedResourceTitle?: string;
};

const CONTROL_API = "/control";

async function parseJsonResponse<T>(response: Response): Promise<T> {
  const raw = await response.text();
  try {
    return JSON.parse(raw) as T;
  } catch {
    if (response.status === 502) {
      throw new Error("PiNAS 控制服务暂时不可用，请稍后刷新页面重试。");
    }
    throw new Error(`服务返回了无法识别的响应（HTTP ${response.status}）。`);
  }
}
const emptySnapshot: Snapshot = {
  schemaVersion: 2,
  snapshotId: "",
  generatedAt: "",
  source: "",
  stats: {
    resources: 0,
    jellyfinGroups: 0,
    qbTasks: 0,
    matchedQbTasks: 0,
    unmatchedQbTasks: 0,
    hrSourceAvailable: false,
    hrActiveTitles: 0,
    hrMatchedQbTasks: 0,
  },
  resources: [],
};
const filterGroups: {
  id: FilterGroupId;
  label: string;
  multi: boolean;
  options: { id: string; label: string; tone?: "warning" }[];
}[] = [
  {
    id: "type",
    label: "资源类型",
    multi: false,
    options: [
      { id: "all", label: "全部资源" },
      { id: "movie", label: "电影" },
      { id: "tv", label: "电视剧" },
    ],
  },
  {
    id: "library",
    label: "媒体库状态",
    multi: false,
    options: [
      { id: "all", label: "全部" },
      { id: "imported", label: "已入库" },
      { id: "not-imported", label: "未入库" },
    ],
  },
  {
    id: "seed",
    label: "做种约束",
    multi: false,
    options: [
      { id: "all", label: "全部" },
      { id: "hr", label: "H&R 保护中", tone: "warning" },
      { id: "none", label: "无做种要求" },
    ],
  },
  {
    id: "flags",
    label: "待处理 / 质量",
    multi: true,
    options: [
      { id: "incomplete", label: "剧集不完整", tone: "warning" },
      { id: "name-pending", label: "名称待确认" },
    ],
  },
];

function createFilterState(): FilterState {
  return { type: "all", library: "all", seed: "all", flags: [] };
}

function matchesFilter(item: Resource, filter: LegacyFilter) {
  if (filter === "all") return true;
  if (filter === "movie") return item.type === "电影";
  if (filter === "tv") return item.type === "电视剧";
  if (filter === "tv-incomplete") return isIncompleteTv(item);
  if (filter === "library") return item.library;
  if (filter === "hr") return item.hr || Boolean(item.hrPending);
  if (filter === "review") {
    return !item.protected && item.qbSummary === "无 qB 任务";
  }
  return item.metadataVerified === false;
}

function isIncompleteTv(item: Resource) {
  if (item.type !== "电视剧") return false;
  return item.episodeIncomplete === true;
}

function episodeGapLabel(item: Resource) {
  if (!item.episodeIncomplete && item.episodeStatus !== "incomplete") return "";
  const missing = Number(item.episodeMissing || 0);
  const episodes = item.episodeMissingEpisodes?.filter(Boolean) || [];
  const detail = episodes.length ? `（${episodes.join("、")}）` : "";
  return `缺 ${missing} 集${detail}`;
}

function matchesFilterState(item: Resource, state: FilterState) {
  if (state.type !== "all" && (state.type === "movie" ? item.type !== "电影" : item.type !== "电视剧")) return false;
  if (state.library === "imported" && !item.library) return false;
  if (state.library === "not-imported" && item.library) return false;
  if (state.seed === "hr" && !matchesFilter(item, "hr")) return false;
  if (state.seed === "none" && !matchesFilter(item, "review")) return false;
  if (state.flags.includes("incomplete") && !matchesFilter(item, "tv-incomplete")) return false;
  if (state.flags.includes("name-pending") && item.metadataVerified !== false) return false;
  return true;
}

function filterOptionCount(resources: Resource[], state: FilterState, group: FilterGroupId, option: string) {
  const candidate: FilterState = { ...state, flags: [...state.flags] };
  if (group === "type") candidate.type = option as FilterState["type"];
  if (group === "library") candidate.library = option as FilterState["library"];
  if (group === "seed") candidate.seed = option as FilterState["seed"];
  if (group === "flags") {
    candidate.flags = option === "all" ? [] : [...new Set([...candidate.flags, option])];
  }
  return resources.filter((item) => matchesFilterState(item, candidate)).length;
}

function formatGiB(size: number) {
  return size >= 1024
    ? `${(size / 1024).toFixed(2)} TB`
    : `${size.toFixed(1)} GB`;
}

function formatBytes(size: number) {
  return formatGiB(size / 1024 ** 3);
}

function actionTitle(mode: ActionMode) {
  if (mode === "pause") return "仅停止做种";
  if (mode === "retire") return "退出做种，保留媒体";
  return "完整删除资源";
}

function actionSummary(mode: ActionMode, selectedSize: number) {
  if (mode === "pause") return "不会释放空间，可随时重新开始做种";
  if (mode === "retire") return "移除 qB 任务，但硬链接媒体仍可播放";
  return `完整删除上限 ${formatGiB(selectedSize)}`;
}

export default function Home() {
  const [snapshot, setSnapshot] = useState<Snapshot>(emptySnapshot);
  const [snapshotLoaded, setSnapshotLoaded] = useState(false);
  const [controlStatus, setControlStatus] =
    useState<ControlStatus>("connecting");
  const [sessionToken, setSessionToken] = useState("");
  const [executionEnabled, setExecutionEnabled] = useState(false);
  const [inventoryCurrent, setInventoryCurrent] = useState(true);
  const [runtimeMode, setRuntimeMode] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [refreshElapsed, setRefreshElapsed] = useState(0);
  const [snapshotError, setSnapshotError] = useState("");
  const [filterState, setFilterState] = useState<FilterState>(createFilterState);
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [safeOnly, setSafeOnly] = useState(false);
  const [sortDescending, setSortDescending] = useState(true);
  const [actionMode, setActionMode] = useState<ActionMode | null>(null);
  const [plan, setPlan] = useState<PublicPlan | null>(null);
  const [planLoading, setPlanLoading] = useState(false);
  const [planError, setPlanError] = useState("");
  const [acknowledgeSiteRisk, setAcknowledgeSiteRisk] = useState(false);
  const [acknowledgeMissingFiles, setAcknowledgeMissingFiles] =
    useState(false);
  const [confirmationOpen, setConfirmationOpen] = useState(false);
  const [executing, setExecuting] = useState(false);
  const [executeError, setExecuteError] = useState("");
  const [executeResult, setExecuteResult] =
    useState<ExecutionResult | null>(null);
  const [recoveryOpen, setRecoveryOpen] = useState(false);
  const [recoveryLoading, setRecoveryLoading] = useState(false);
  const [recoveries, setRecoveries] = useState<RecoveryStatus[]>([]);
  const [recoveryError, setRecoveryError] = useState("");
  const [recoveryTarget, setRecoveryTarget] =
    useState<RecoveryStatus | null>(null);
  const [recoveryAction, setRecoveryAction] =
    useState<RecoveryAction | null>(null);
  const [recoveryPhraseInput, setRecoveryPhraseInput] = useState("");
  const [recovering, setRecovering] = useState(false);
  const [recoveryCompleted, setRecoveryCompleted] = useState(false);
  const [recoveryRefreshPending, setRecoveryRefreshPending] = useState(false);
  const [protectionGapOpen, setProtectionGapOpen] = useState(false);
  const [protectionGapLoading, setProtectionGapLoading] = useState(false);
  const [protectionGaps, setProtectionGaps] = useState<ProtectionGap[]>([]);
  const [protectionGapError, setProtectionGapError] = useState("");
  const [clock, setClock] = useState(() => Date.now());
  const planRequestSequence = useRef(0);

  const acceptSnapshot = useCallback((nextSnapshot: Snapshot) => {
    if (
      nextSnapshot.schemaVersion !== 2 ||
      !nextSnapshot.snapshotId ||
      !Array.isArray(nextSnapshot.resources)
    ) {
      throw new Error("数据格式不受支持");
    }
    setSnapshot(nextSnapshot);
    setSnapshotLoaded(true);
    const availableIds = new Set(nextSnapshot.resources.map((item) => item.id));
    setSelected((current) => current.filter((id) => availableIds.has(id)));
    setSnapshotError("");
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;

    const readJson = async <T,>(url: string): Promise<T> => {
      const response = await fetch(url, {
        cache: "no-store",
        signal: controller.signal,
      });
      const payload = await parseJsonResponse<T>(response);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return payload;
    };

    const readPublicFallback = async () => {
      const fallback = await readJson<Snapshot>(
        `/data/resource-snapshot.json?t=${Date.now()}`,
      );
      if (!cancelled) acceptSnapshot(fallback);
    };

    const connect = async () => {
      try {
        const payload = await readJson<{
          sessionToken: string;
          executionEnabled: boolean;
          inventoryCurrent: boolean;
          runtimeMode?: string;
        }>(`${CONTROL_API}/v1/session`);
        if (cancelled) return;
        setSessionToken(payload.sessionToken);
        setExecutionEnabled(payload.executionEnabled);
        setInventoryCurrent(payload.inventoryCurrent);
        setRuntimeMode(payload.runtimeMode || "");
        setControlStatus("ready");
        try {
          const current = await readJson<{ snapshot?: Snapshot }>(
            `${CONTROL_API}/v1/snapshot`,
          );
          if (!current.snapshot) {
            throw new Error("控制服务响应缺少快照");
          }
          if (!cancelled) acceptSnapshot(current.snapshot);
        } catch (error) {
          if (
            error instanceof DOMException &&
            error.name === "AbortError"
          ) {
            return;
          }
          try {
            await readPublicFallback();
          } catch (fallbackError) {
            if (
              fallbackError instanceof DOMException &&
              fallbackError.name === "AbortError"
            ) {
              return;
            }
            if (!cancelled) {
              setSnapshotError(
                fallbackError instanceof Error
                  ? `无法读取资源清单：${fallbackError.message}`
                  : "无法读取资源清单",
              );
            }
          }
        }
      } catch (error) {
        if (
          error instanceof DOMException &&
          error.name === "AbortError"
        ) {
          return;
        }
        if (!cancelled) setControlStatus("offline");
        try {
          await readPublicFallback();
        } catch (fallbackError) {
          if (
            fallbackError instanceof DOMException &&
            fallbackError.name === "AbortError"
          ) {
            return;
          }
          if (!cancelled) {
            setSnapshotError(
              fallbackError instanceof Error
                ? `无法读取资源清单：${fallbackError.message}`
                : "无法读取资源清单",
            );
          }
        }
      }
    };

    void connect();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [acceptSnapshot]);

  useEffect(() => {
    if (!actionMode) return;
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [actionMode]);

  useEffect(() => {
    if (!refreshing) {
      setRefreshElapsed(0);
      return;
    }
    const startedAt = Date.now();
    setRefreshElapsed(0);
    const timer = window.setInterval(() => {
      setRefreshElapsed(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [refreshing]);

  const resources = snapshot.resources;
  const hrGap = Math.max(
    0,
    snapshot.stats.hrMissingQbTasks ??
      snapshot.stats.hrActiveTitles - snapshot.stats.hrMatchedQbTasks,
  );
  const hrUnassigned =
    snapshot.stats.hrMissingUnassigned ??
    snapshot.stats.hrMissingUncovered ??
    0;
  const unresolvedTransactions =
    snapshot.stats.unresolvedTransactions ?? 0;
  const filters = useMemo(
    () =>
      filterGroups.map((group) => ({
        ...group,
        options: group.options.map((option) => ({
          ...option,
          count: filterOptionCount(resources, filterState, group.id, option.id),
        })),
      })),
    [resources, filterState],
  );
  const activeFilterChips = useMemo(
    () =>
      filterGroups.flatMap((group) =>
        group.options
          .filter((option) =>
            option.id !== "all" &&
            (group.id === "flags"
              ? filterState.flags.includes(option.id)
              : filterState[group.id] === option.id),
          )
          .map((option) => ({ group: group.id, id: option.id, label: option.label })),
      ),
    [filterState],
  );
  const visible = useMemo(() => {
    const query = search.trim().toLowerCase();
    return resources
      .filter((item) => {
        const filterMatch = matchesFilterState(item, filterState);
        const safeMatch =
          !safeOnly ||
          (!item.protected && item.qbSummary === "无 qB 任务");
        const text =
          `${item.title} ${item.englishTitle} ${item.edition} ${item.siteSummary}`.toLowerCase();
        return filterMatch && safeMatch && (!query || text.includes(query));
      })
      .sort((left, right) => {
        const sizeOrder = sortDescending
          ? right.size - left.size
          : left.size - right.size;
        return sizeOrder || left.title.localeCompare(right.title, "zh-CN");
      });
  }, [
    filterState,
    resources,
    safeOnly,
    search,
    sortDescending,
  ]);
  const selectedItems = resources.filter((item) => selected.includes(item.id));
  const selectedSize = selectedItems.reduce((total, item) => total + item.size, 0);

  const loadSnapshot = async () => {
    setRefreshing(true);
    try {
      if (controlStatus === "ready" && sessionToken) {
        const response = await fetch(`${CONTROL_API}/v1/refresh`, {
          method: "POST",
          cache: "no-store",
          headers: {
            "Content-Type": "application/json",
            "X-PiNAS-Session": sessionToken,
          },
          body: "{}",
        });
        const payload = await parseJsonResponse<{
          ok: boolean;
          snapshot?: Snapshot;
          error?: { message: string };
        }>(response);
        if (!response.ok || !payload.snapshot) {
          throw new Error(payload.error?.message || `HTTP ${response.status}`);
        }
        acceptSnapshot(payload.snapshot);
        setInventoryCurrent(true);
      } else {
        const response = await fetch(
          `/data/resource-snapshot.json?t=${Date.now()}`,
          { cache: "no-store" },
        );
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        acceptSnapshot(await parseJsonResponse<Snapshot>(response));
      }
    } catch (error) {
      if (controlStatus === "ready") setInventoryCurrent(false);
      setSnapshotError(
        error instanceof Error
          ? error.message
          : "刷新失败，继续显示上次快照",
      );
    } finally {
      setRefreshing(false);
    }
  };

  const requestPlan = async (
    mode: ActionMode,
    siteRiskAcknowledged: boolean,
    missingFilesAcknowledged: boolean,
  ) => {
    const requestId = ++planRequestSequence.current;
    setPlanLoading(true);
    setPlan(null);
    setPlanError("");
    if (controlStatus !== "ready" || !sessionToken) {
      if (requestId === planRequestSequence.current) {
        setPlanLoading(false);
        setPlanError("本地控制服务未启动，当前只能查看资源。");
      }
      return;
    }
    try {
      const response = await fetch(`${CONTROL_API}/v1/plan`, {
        method: "POST",
        cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          "X-PiNAS-Session": sessionToken,
        },
        body: JSON.stringify({
          snapshotId: snapshot.snapshotId,
          resourceIds: selected,
          mode,
          acknowledgeSiteRisk: siteRiskAcknowledged,
          acknowledgeMissingFiles: missingFilesAcknowledged,
        }),
      });
      const payload = await parseJsonResponse<{
        ok: boolean;
        plan?: PublicPlan;
        error?: { message: string };
      }>(response);
      if (!response.ok || !payload.plan) {
        throw new Error(payload.error?.message || `HTTP ${response.status}`);
      }
      if (requestId === planRequestSequence.current) {
        setPlan(payload.plan);
        setClock(Date.now());
      }
    } catch (error) {
      if (requestId === planRequestSequence.current) {
        setPlanError(
          error instanceof Error ? error.message : "无法生成执行计划。",
        );
      }
    } finally {
      if (requestId === planRequestSequence.current) {
        setPlanLoading(false);
      }
    }
  };

  const toggleSelected = (item: Resource) => {
    if (item.protected) return;
    setSelected((current) =>
      current.includes(item.id)
        ? current.filter((id) => id !== item.id)
        : [...current, item.id],
    );
  };

  const selectSharedResources = (item: Resource) => {
    const relatedIds = (item.sharedHardlinkResources || [])
      .map((related) => related.id)
      .filter((id) => resources.some((candidate) => candidate.id === id && !candidate.protected));
    if (!relatedIds.length) return;
    setSelected((current) => [...new Set([...current, ...relatedIds])]);
  };

  const isFilterActive = (group: FilterGroupId, option: string) =>
    group === "flags"
      ? filterState.flags.includes(option)
      : filterState[group] === option;

  const selectFilter = (group: FilterGroupId, option: string) => {
    setFilterState((current) => {
      if (group === "flags") {
        return {
          ...current,
          flags: current.flags.includes(option)
            ? current.flags.filter((id) => id !== option)
            : [...current.flags, option],
        };
      }
      return { ...current, [group]: option } as FilterState;
    });
  };

  const clearFilters = () => setFilterState(createFilterState());

  const clearFilterChip = (group: FilterGroupId, option: string) => {
    if (group === "flags") {
      setFilterState((current) => ({
        ...current,
        flags: current.flags.filter((id) => id !== option),
      }));
      return;
    }
    setFilterState((current) => ({ ...current, [group]: "all" }) as FilterState);
  };

  const openPlan = (mode: ActionMode) => {
    setClock(Date.now());
    setActionMode(mode);
    setAcknowledgeSiteRisk(false);
    setAcknowledgeMissingFiles(false);
    setConfirmationOpen(false);
    setExecuteError("");
    setExecuteResult(null);
    void requestPlan(mode, false, false);
  };

  const closePlan = () => {
    if (executing) return;
    planRequestSequence.current += 1;
    setActionMode(null);
    setPlan(null);
    setPlanError("");
    setAcknowledgeSiteRisk(false);
    setAcknowledgeMissingFiles(false);
    setConfirmationOpen(false);
    setExecuteError("");
  };

  const toggleSiteRisk = (checked: boolean) => {
    setAcknowledgeSiteRisk(checked);
    setConfirmationOpen(false);
    setExecuteError("");
    if (actionMode) {
      void requestPlan(actionMode, checked, acknowledgeMissingFiles);
    }
  };

  const toggleMissingFileAcknowledgement = (checked: boolean) => {
    setAcknowledgeMissingFiles(checked);
    setConfirmationOpen(false);
    setExecuteError("");
    if (actionMode) {
      void requestPlan(actionMode, acknowledgeSiteRisk, checked);
    }
  };

  const executePlan = async () => {
    if (
      !plan ||
      !sessionToken ||
      executing ||
      Date.parse(plan.expiresAt) <= Date.now()
    ) {
      setExecuteError("安全预演已过期，请关闭窗口后重新生成。");
      return;
    }
    setExecuting(true);
    setExecuteError("");
    try {
      const response = await fetch(`${CONTROL_API}/v1/execute`, {
        method: "POST",
        cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          "X-PiNAS-Session": sessionToken,
        },
        body: JSON.stringify({
          planId: plan.planId,
          // Keep the server-side phrase internal while the UI uses a
          // deliberate two-click confirmation.
          confirmPhrase: plan.confirmPhrase,
        }),
      });
      const payload = await parseJsonResponse<{
        ok: boolean;
        result?: ExecutionResult;
        error?: { message: string; plan?: PublicPlan };
      }>(response);
      if (!response.ok || !payload.result) {
        if (payload.error?.plan) {
          setPlan(payload.error.plan);
          setClock(Date.now());
          setConfirmationOpen(false);
        }
        throw new Error(payload.error?.message || `HTTP ${response.status}`);
      }
      setExecuteResult(payload.result);
      setSelected([]);
      if (plan.mode === "delete") {
        const deletedIds = new Set(plan.resources.map((item) => item.id));
        setSnapshot((current) => ({
          ...current,
          resources: current.resources.filter(
            (item) => !deletedIds.has(item.id),
          ),
        }));
      }
      if (payload.result.snapshotRefreshPending) {
        setInventoryCurrent(false);
        setSnapshotError(
          plan.mode === "delete"
            ? "操作已完成，已从当前列表移除；刷新资源清单后同步其余统计。"
            : "操作已完成，但最新资源清单刷新失败；新操作已锁定。",
        );
      } else {
        try {
          const snapshotResponse = await fetch(`${CONTROL_API}/v1/snapshot`, {
            cache: "no-store",
          });
          if (!snapshotResponse.ok) {
            throw new Error(`HTTP ${snapshotResponse.status}`);
          }
          const snapshotPayload = await parseJsonResponse<{
            snapshot?: Snapshot;
          }>(snapshotResponse);
          if (!snapshotPayload.snapshot) {
            throw new Error("最新清单响应缺少快照");
          }
          acceptSnapshot(snapshotPayload.snapshot);
          setInventoryCurrent(true);
        } catch {
          setInventoryCurrent(false);
          setSnapshotError(
            "操作已经完成，但页面未取得最新资源清单；请手动刷新后继续。",
          );
        }
      }
    } catch (error) {
      setExecuteError(
        error instanceof Error ? error.message : "执行失败，未确认操作结果。",
      );
    } finally {
      setExecuting(false);
    }
  };

  const closeRecovery = () => {
    if (recovering) return;
    setRecoveryOpen(false);
    setRecoveryTarget(null);
    setRecoveryAction(null);
    setRecoveryPhraseInput("");
    setRecoveryError("");
    setRecoveryCompleted(false);
    setRecoveryRefreshPending(false);
  };

  const loadRecoveries = async () => {
    setRecoveryOpen(true);
    setRecoveryLoading(true);
    setRecoveryError("");
    setRecoveryTarget(null);
    setRecoveryAction(null);
    setRecoveryPhraseInput("");
    setRecoveryCompleted(false);
    setRecoveryRefreshPending(false);
    if (controlStatus !== "ready" || !sessionToken) {
      setRecoveryLoading(false);
      setRecoveryError("本地控制服务未启动，无法核对恢复状态。");
      return;
    }
    try {
      const response = await fetch(`${CONTROL_API}/v1/recovery`, {
        cache: "no-store",
        headers: {
          "X-PiNAS-Session": sessionToken,
        },
      });
      const payload = (await response.json()) as {
        ok: boolean;
        recoveries?: RecoveryStatus[];
        error?: { message: string };
      };
      if (!response.ok || !payload.recoveries) {
        throw new Error(payload.error?.message || `HTTP ${response.status}`);
      }
      setRecoveries(payload.recoveries);
    } catch (error) {
      setRecoveryError(
        error instanceof Error ? error.message : "无法读取未完成事务。",
      );
    } finally {
      setRecoveryLoading(false);
    }
  };

  const chooseRecovery = (
    item: RecoveryStatus,
    action: RecoveryAction,
  ) => {
    setRecoveryTarget(item);
    setRecoveryAction(action);
    setRecoveryPhraseInput("");
    setRecoveryError("");
  };

  const executeRecovery = async () => {
    if (
      !recoveryTarget ||
      !recoveryAction ||
      !sessionToken ||
      recovering
    ) {
      return;
    }
    setRecovering(true);
    setRecoveryError("");
    try {
      const response = await fetch(`${CONTROL_API}/v1/recovery`, {
        method: "POST",
        cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          "X-PiNAS-Session": sessionToken,
        },
        body: JSON.stringify({
          planId: recoveryTarget.planId,
          action: recoveryAction,
          confirmPhrase: recoveryPhraseInput,
        }),
      });
      const payload = await parseJsonResponse<{
        ok: boolean;
        result?: {
          phase: string;
          snapshotRefreshPending: boolean;
        };
        error?: { message: string };
      }>(response);
      if (!response.ok || !payload.result) {
        throw new Error(payload.error?.message || `HTTP ${response.status}`);
      }
      setRecoveryCompleted(true);
      setRecoveryRefreshPending(payload.result.snapshotRefreshPending);
      if (payload.result.snapshotRefreshPending) {
        setInventoryCurrent(false);
        setSnapshotError("恢复已完成，但最新资源清单刷新失败；新操作已锁定。");
      } else {
        try {
          const snapshotResponse = await fetch(`${CONTROL_API}/v1/snapshot`, {
            cache: "no-store",
          });
          if (!snapshotResponse.ok) {
            throw new Error(`HTTP ${snapshotResponse.status}`);
          }
          const snapshotPayload = await parseJsonResponse<{
            snapshot?: Snapshot;
          }>(snapshotResponse);
          if (!snapshotPayload.snapshot) {
            throw new Error("最新清单响应缺少快照");
          }
          acceptSnapshot(snapshotPayload.snapshot);
          setInventoryCurrent(true);
        } catch {
          setInventoryCurrent(false);
          setSnapshotError(
            "恢复已经完成，但页面未取得最新资源清单；请手动刷新后继续。",
          );
        }
      }
    } catch (error) {
      setRecoveryError(
        error instanceof Error
          ? error.message
          : "事务恢复失败，全部新操作仍保持锁定。",
      );
    } finally {
      setRecovering(false);
    }
  };

  const expectedRecoveryPhrase =
    recoveryTarget && recoveryAction
      ? recoveryAction === "rollback"
        ? recoveryTarget.rollbackPhrase
        : recoveryTarget.finalizePhrase
      : "";

  const loadProtectionGaps = async () => {
    setProtectionGapOpen(true);
    setProtectionGapLoading(true);
    setProtectionGapError("");
    if (controlStatus !== "ready" || !sessionToken) {
      setProtectionGapLoading(false);
      setProtectionGapError("本地控制服务未启动，无法读取 H&R 明细。");
      return;
    }
    try {
      const response = await fetch(`${CONTROL_API}/v1/protection-gaps`, {
        cache: "no-store",
        headers: {
          "X-PiNAS-Session": sessionToken,
        },
      });
      const payload = await parseJsonResponse<{
        ok: boolean;
        gaps?: ProtectionGap[];
        error?: { message: string };
      }>(response);
      if (!response.ok || !payload.gaps) {
        throw new Error(payload.error?.message || `HTTP ${response.status}`);
      }
      setProtectionGaps(payload.gaps);
    } catch (error) {
      setProtectionGapError(
        error instanceof Error ? error.message : "无法读取 H&R 缺口。",
      );
    } finally {
      setProtectionGapLoading(false);
    }
  };

  const topStatus =
    !snapshotLoaded
      ? "正在读取资源清单"
      : !inventoryCurrent
      ? "资源清单待刷新"
      : controlStatus === "offline"
      ? "控制服务未启动"
      : controlStatus === "connecting"
        ? "正在连接控制服务"
        : executionEnabled
          ? "执行模式已启用"
          : runtimeMode === "pi-local"
            ? "Pi 只读模式"
            : "只读模式";

  const topStatusTone = snapshotError || !inventoryCurrent
    ? "locked"
    : executionEnabled
      ? "enabled"
      : controlStatus === "offline" || controlStatus === "connecting"
        ? "offline"
        : "ready";
  const refreshMessage = !refreshing
    ? ""
    : refreshElapsed < 5
      ? "正在读取 NAS 只读快照…"
      : refreshElapsed < 30
        ? `正在核对 qB、Jellyfin 与 H&R（已等待 ${refreshElapsed} 秒）`
        : `远端 H&R 探测可能需要数分钟（已等待 ${refreshElapsed} 秒），请保持页面打开。`;
  const planExpired = Boolean(
    plan && Date.parse(plan.expiresAt) <= clock,
  );

  return (
    <main className="list-app">
      <header className="compact-topbar">
        <div className="compact-brand">
          <span>收</span>
          <div>
            <strong>NAS 清理台</strong>
            <small>资源清单</small>
          </div>
        </div>
        <div
          className={`safe-note ${topStatusTone}`}
        >
          <span>盾</span>
          <p>
            <strong>{snapshotError || topStatus}</strong>
            <small>
              {snapshotLoaded
                ? `更新于 ${snapshot.generatedAt.slice(5, 16).replace("T", " ")}`
                : "尚未显示任何资源"}
            </small>
          </p>
        </div>
      </header>

      <section className="list-workspace">
        <section className="list-controls">
          <label className="list-search">
            <span>⌕</span>
            <input
              aria-label="搜索资源"
              placeholder="搜索电影、剧集、季度或站点"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
          <div className="control-spacer" />
          <label className="impact-toggle">
            <input
              type="checkbox"
              checked={safeOnly}
              onChange={(event) => setSafeOnly(event.target.checked)}
            />
            <span />
            仅看无做种限制
          </label>
          <button
            className="sort-control"
            type="button"
            aria-label="切换实际占用排序"
            onClick={() => setSortDescending((current) => !current)}
          >
            实际占用：{sortDescending ? "从大到小" : "从小到大"}
          </button>
          <button
            className="refresh-control"
            type="button"
            aria-label={refreshing ? "正在刷新资源清单" : "刷新资源清单"}
            aria-busy={refreshing}
            title={refreshMessage || "刷新资源清单"}
            disabled={refreshing}
            onClick={() => void loadSnapshot()}
          >
            {refreshing ? "…" : "↻"}
          </button>
          {refreshing && (
            <p className="refresh-feedback" role="status" aria-live="polite">
              {refreshMessage}
            </p>
          )}
        </section>

        {unresolvedTransactions > 0 && (
          <button
            type="button"
            className="recovery-notice"
            onClick={() => void loadRecoveries()}
          >
            <span>!</span>
            <p>
              <strong>
                检测到 {unresolvedTransactions} 个未完成清理事务
              </strong>
              <small>
                全部新操作已锁定；先核对 qB 与隔离文件，再选择回滚或完成原事务。
              </small>
            </p>
            <b>查看恢复状态</b>
          </button>
        )}

        {unresolvedTransactions === 0 && hrGap > 0 && (
          <button
            type="button"
            className={`recovery-notice hr-gap-notice ${
              hrUnassigned ? "critical" : ""
            }`}
            onClick={() => void loadProtectionGaps()}
          >
            <span>H</span>
            <p>
              <strong>
                {hrGap} 个学校站 H&R 尚未恢复完成
              </strong>
              <small>
                {hrUnassigned
                  ? `${hrUnassigned} 个未精确关联到媒体；不影响无关资源独立审阅。`
                  : "缺失任务均已关联到锁定媒体，其他资源可独立审阅。"}
              </small>
            </p>
            <b>查看缺口明细</b>
          </button>
        )}

        <section className="resource-filter-panel" aria-label="资源筛选">
          {filters.map((group) => (
            <div className="resource-filter-group" key={group.id}>
              <div className="resource-filter-label">
                {group.label}
                <small>{group.multi ? "可多选" : "单选"}</small>
              </div>
              <div className="resource-filter-options">
                {group.options.map((option) => (
                  <button
                    type="button"
                    key={option.id}
                    className={`resource-filter-option ${isFilterActive(group.id, option.id) ? "active" : ""} ${option.tone === "warning" ? "warning" : ""}`}
                    aria-pressed={isFilterActive(group.id, option.id)}
                    onClick={() => selectFilter(group.id, option.id)}
                  >
                    {option.label}
                    <span>{option.count}</span>
                  </button>
                ))}
              </div>
            </div>
          ))}
          <div className="resource-filter-footer">
            <div className="resource-filter-chips" aria-live="polite">
              {activeFilterChips.length === 0 ? (
                <span className="resource-filter-caption">当前筛选：全部资源</span>
              ) : (
                <>
                  <span className="resource-filter-caption">当前筛选</span>
                  {activeFilterChips.map((chip) => (
                    <button
                      type="button"
                      className="resource-filter-chip"
                      key={`${chip.group}-${chip.id}`}
                      onClick={() => clearFilterChip(chip.group, chip.id)}
                    >
                      {chip.label} ×
                    </button>
                  ))}
                </>
              )}
            </div>
            <div className="resource-filter-result">
              <strong>{visible.length}</strong> 条结果
              {activeFilterChips.length > 0 && (
                <button type="button" onClick={clearFilters}>清除筛选</button>
              )}
            </div>
          </div>
          <p className="resource-filter-help">同组条件单选；不同组条件按 AND 组合。待处理 / 质量标签可以叠加。</p>
        </section>

        <section className="resource-panel">
          <div className="resource-table">
            <div className="table-head">
              <span className="check-cell" />
              <span>资源</span>
              <span>媒体库</span>
              <span>做种与保护</span>
              <span>实际占用</span>
              <span>完整删除影响</span>
            </div>

            {visible.map((item) => {
              const isSelected = selected.includes(item.id);
              const selectionBlocked = item.protected;
              return (
                <article className="resource-group" key={item.id}>
                  <div className="resource-row">
                    <label
                      className={`row-check ${selectionBlocked ? "disabled" : ""}`}
                      aria-label={
                        selectionBlocked
                          ? `${item.title} ${item.edition} 暂不可清理`
                          : `${isSelected ? "取消选择" : "选择"} ${item.title} ${item.edition}`
                      }
                    >
                      <input
                        type="checkbox"
                        checked={isSelected}
                        disabled={selectionBlocked}
                        onChange={() => toggleSelected(item)}
                      />
                      <span>
                        {selectionBlocked ? "锁" : isSelected ? "✓" : ""}
                      </span>
                    </label>

                    <div className="resource-title">
                      <div>
                        <strong>{item.title}</strong>
                        <p className="english-name">{item.englishTitle}</p>
                        <small>
                          {[item.type, item.year, item.edition]
                            .filter(Boolean)
                            .join(" · ")}
                        </small>
                      </div>
                    </div>

                    <div className="library-cell">
                      <strong>{item.librarySummary}</strong>
                      <span>{item.libraryDetail}</span>
                      {episodeGapLabel(item) ? (
                        <span>{episodeGapLabel(item)}</span>
                      ) : null}
                    </div>

                    <div className="seed-cell">
                      {item.seedTasks?.length ? (
                        item.seedTasks.map((task) => (
                          <div
                            className={`seed-task ${task.tone}`}
                            key={`${task.site}-${task.scope}-${task.status}-${task.tone}-${task.count ?? 1}`}
                          >
                            <i>{task.status}</i>
                            <strong>{task.site}</strong>
                            <span>
                              {task.scope}
                              {task.count && task.count > 1
                                ? ` · ${task.count} 个任务`
                                : ""}
                            </span>
                          </div>
                        ))
                      ) : (
                        <div className="seed-summary">
                          <strong>{item.qbSummary}</strong>
                          <span>{item.siteSummary}</span>
                        </div>
                      )}
                    </div>

                    <div className="size-cell">
                      <strong>{item.sizeLabel}</strong>
                      <span>{item.reclaimLabel}</span>
                    </div>

                    <div
                      className={`impact-cell ${selectionBlocked ? "danger" : ""}`}
                    >
                      <strong>{item.impactTitle}</strong>
                      {selectionBlocked && item.lockReason ? (
                        <span>锁定原因：{item.lockReason}</span>
                      ) : null}
                      <span>{item.impactDetail}</span>
                      {item.sharedHardlinkResources?.length ? (
                        <div className="shared-hardlink-impact">
                          <strong>共享硬链接影响</strong>
                          <span>
                            与 {item.sharedHardlinkResources.slice(0, 3).map((related) => (
                              `${related.title}${related.edition ? `（${related.edition}）` : ""}${related.protected ? " · 锁定" : ""}`
                            )).join("、")}
                            {item.sharedHardlinkResources.length > 3
                              ? ` 等 ${item.sharedHardlinkResources.length} 项`
                              : ""}
                            共用文件；完整删除需同时纳入并重新预演。
                          </span>
                          {item.sharedHardlinkResources.some((related) => {
                            const candidate = resources.find((resource) => resource.id === related.id);
                            return candidate && !candidate.protected;
                          }) ? (
                            <button
                              type="button"
                              className="shared-hardlink-button"
                              onClick={() => selectSharedResources(item)}
                            >
                              加入可选关联资源
                            </button>
                          ) : (
                            <span>关联资源含锁定项，不能单独清理。</span>
                          )}
                        </div>
                      ) : null}
                    </div>
                  </div>
                </article>
              );
            })}

            {!visible.length && (
              <div className="list-empty">
                <strong>没有符合条件的资源</strong>
                <span>尝试取消筛选或更换关键词。</span>
              </div>
            )}
          </div>
        </section>
      </section>

      <aside className={`list-selection ${selected.length ? "visible" : ""}`}>
        <span className="selected-number">{selected.length}</span>
        <p>
          <strong>已加入清理计划</strong>
          <small>完整删除上限 {formatGiB(selectedSize)}</small>
        </p>
        <button
          type="button"
          className="clear-plan"
          onClick={() => setSelected([])}
        >
          清空
        </button>
        <div className="cleanup-levels">
          <button type="button" onClick={() => openPlan("pause")}>
            <strong>停止做种</strong>
            <small>保留 qB 任务和全部文件</small>
          </button>
          <button type="button" onClick={() => openPlan("retire")}>
            <strong>退出做种</strong>
            <small>移除 qB 任务，媒体仍保留</small>
          </button>
          <button
            type="button"
            className="delete-level"
            onClick={() => openPlan("delete")}
          >
            <strong>完整删除</strong>
            <small>移除任务、媒体和全部链接</small>
          </button>
        </div>
      </aside>

      {protectionGapOpen && (
        <div className="action-backdrop">
          <button
            type="button"
            className="backdrop-dismiss"
            aria-label="关闭 H&R 缺口"
            onClick={() => setProtectionGapOpen(false)}
          />
          <section
            className="action-dialog protection-gap-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="protection-gap-title"
          >
            <header>
              <div>
                <span>学校站保护 · 实时缺口</span>
                <h2 id="protection-gap-title">H&R 任务核对</h2>
              </div>
              <button
                type="button"
                aria-label="关闭 H&R 缺口"
                onClick={() => setProtectionGapOpen(false)}
              >
                ×
              </button>
            </header>

            <div
              className={`level-explain ${
                hrUnassigned ? "delete" : "retire"
              }`}
            >
              <strong>
                {hrUnassigned
                  ? "未定位 H&R 只作风险提示，不全局锁定"
                  : "缺失任务已定向锁定，不再影响无关资源"}
              </strong>
              <span>
                这里显示的是学校站当前 H&R 与本机 qB 精确
                infohash 对照结果，不使用模糊标题冒充已匹配。
              </span>
            </div>

            {protectionGapLoading && (
              <p className="plan-loading">正在读取私有保护明细…</p>
            )}
            <div className="protection-gap-list">
              {protectionGaps.map((item) => (
                <article key={item.title}>
                  <p>
                    <strong>{item.title}</strong>
                    <small>
                      {item.linkedResourceTitle
                        ? `媒体库中已锁定：${item.linkedResourceTitle}`
                        : item.qbTaskPresent
                          ? "官方任务已在 qB，等待下载与 100% 重检"
                          : "学校站 H&R 任务当前不在 qB"}
                    </small>
                  </p>
                  <b
                    className={
                      item.linkedResourceTitle ? "covered" : ""
                    }
                  >
                    {item.linkedResourceTitle
                      ? "媒体已锁定 · qB 待恢复"
                      : item.qbTaskPresent
                        ? "qB 下载未完成 · 保持保护"
                      : item.coveredByCandidate
                        ? "精确 payload 候选 · 待重检"
                        : "未定位恢复来源"}
                  </b>
                </article>
              ))}
            </div>
            {protectionGapError && (
              <p className="execute-error" role="alert">
                {protectionGapError}
              </p>
            )}
            <button
              type="button"
              className="confirm-preview"
              onClick={() => setProtectionGapOpen(false)}
            >
              知道了
            </button>
          </section>
        </div>
      )}

      {recoveryOpen && (
        <div className="action-backdrop">
          <button
            type="button"
            className="backdrop-dismiss"
            aria-label="关闭恢复窗口"
            onClick={closeRecovery}
          />
          <section
            className="action-dialog recovery-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="recovery-title"
          >
            <header>
              <div>
                <span>异常事务 · 确定性恢复</span>
                <h2 id="recovery-title">恢复未完成的清理</h2>
              </div>
              <button
                type="button"
                aria-label="关闭恢复窗口"
                onClick={closeRecovery}
              >
                ×
              </button>
            </header>

            <div className="level-explain delete">
              <strong>恢复完成前，三档清理都会保持锁定</strong>
              <span>
                系统只根据 qB 当前任务、预写路径映射和文件 inode
                决定可用方向，不会让你手工选择文件。
              </span>
            </div>

            {recoveryLoading && (
              <p className="plan-loading">正在核对 qB 与隔离文件…</p>
            )}

            {!recoveryLoading && !recoveryError && !recoveries.length && (
              <div className="plan-passed">
                <strong>没有未完成事务</strong>
                <span>刷新资源清单后即可继续使用。</span>
              </div>
            )}

            <div className="recovery-list">
              {recoveries.map((item) => {
                const canRollback =
                  item.tasksPresent === item.taskCount &&
                  item.tasksAbsent === 0;
                const canFinalize =
                  item.tasksAbsent === item.taskCount &&
                  item.tasksPresent === 0;
                return (
                  <article
                    className={
                      recoveryTarget?.planId === item.planId
                        ? "selected"
                        : ""
                    }
                    key={item.planId}
                  >
                    <header>
                      <p>
                        <strong>
                          {item.mode === "delete"
                            ? "完整删除事务"
                            : "退出做种事务"}
                        </strong>
                        <small>
                          阶段 {item.phase} · 编号{" "}
                          {item.planId.slice(-8)}
                        </small>
                      </p>
                      <b>
                        qB {item.tasksPresent}/{item.taskCount} 仍在
                      </b>
                    </header>
                    <div>
                      <span>原路径 {item.filesAtSource}</span>
                      <span>隔离区 {item.filesQuarantined}</span>
                      <span>已释放 {item.filesAlreadyGone}</span>
                    </div>
                    {canRollback ? (
                      <button
                        type="button"
                        onClick={() => chooseRecovery(item, "rollback")}
                      >
                        回滚到执行前
                      </button>
                    ) : canFinalize ? (
                      <button
                        type="button"
                        className="danger-execute"
                        onClick={() => chooseRecovery(item, "finalize")}
                      >
                        完成原清理事务
                      </button>
                    ) : (
                      <p className="recovery-manual">
                        qB 状态混合或不可判定，自动恢复继续保持禁用。
                      </p>
                    )}
                  </article>
                );
              })}
            </div>

            {recoveryTarget && recoveryAction && !recoveryCompleted && (
              <section className="final-confirmation recovery-confirmation">
                <label htmlFor="recovery-phrase">
                  {recoveryAction === "rollback"
                    ? "将隔离文件恢复原路径，并恢复 qB 原运行态"
                    : "qB 任务已退出；将释放仍在隔离区的文件"}
                </label>
                <code>{expectedRecoveryPhrase}</code>
                <input
                  id="recovery-phrase"
                  autoComplete="off"
                  spellCheck={false}
                  value={recoveryPhraseInput}
                  disabled={recovering}
                  onChange={(event) =>
                    setRecoveryPhraseInput(event.target.value)
                  }
                  placeholder="逐字输入恢复确认短语"
                />
                <div>
                  <button
                    type="button"
                    disabled={recovering}
                    onClick={() => {
                      setRecoveryTarget(null);
                      setRecoveryAction(null);
                      setRecoveryPhraseInput("");
                      setRecoveryError("");
                    }}
                  >
                    返回
                  </button>
                  <button
                    type="button"
                    className={
                      recoveryAction === "finalize"
                        ? "danger-execute"
                        : ""
                    }
                    disabled={
                      !executionEnabled ||
                      recovering ||
                      recoveryPhraseInput !== expectedRecoveryPhrase
                    }
                    onClick={() => void executeRecovery()}
                  >
                    {!executionEnabled
                      ? "执行引擎尚未启用"
                      : recovering
                        ? "正在再次核对…"
                        : recoveryAction === "rollback"
                          ? "确认回滚"
                          : "确认完成事务"}
                  </button>
                </div>
              </section>
            )}

            {recoveryError && (
              <p className="execute-error recovery-error" role="alert">
                {recoveryError}
              </p>
            )}

            {recoveryCompleted && (
              <div className="execution-success" role="status">
                <strong>事务恢复已完成</strong>
                <span>
                  {recoveryRefreshPending
                    ? "最新资源清单刷新失败；新操作保持锁定，请手动刷新。"
                    : "真实资源清单已经重新采集，安全锁将按新状态解除。"}
                </span>
                <button type="button" onClick={closeRecovery}>
                  完成
                </button>
              </div>
            )}
          </section>
        </div>
      )}

      {actionMode && (
        <div className="action-backdrop">
          <button
            type="button"
            className="backdrop-dismiss"
            aria-label="关闭清理预演"
            onClick={closePlan}
          />
          <section
            className="action-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="action-title"
          >
            <header>
              <div>
                <span>清理等级 · 真实预演</span>
                <h2 id="action-title">{actionTitle(actionMode)}</h2>
              </div>
              <button type="button" aria-label="关闭" onClick={closePlan}>
                ×
              </button>
            </header>

            <div className={`level-explain ${actionMode}`}>
              <strong>
                {plan && actionMode === "delete"
                  ? plan.canExecute
                    ? `已核算可释放${plan.acknowledgeMissingFiles ? "（不含已确认缺失入口）" : ""} ${formatBytes(plan.estimatedReclaimBytes)}`
                    : "安全可释放暂不可核算"
                  : actionSummary(actionMode, selectedSize)}
              </strong>
              <span>
                {actionMode === "pause"
                  ? "只改变 qB 运行状态，不删除任务或文件。"
                  : actionMode === "retire"
                    ? "移除 qB 任务但不删除文件，媒体库仍可播放。"
                    : "只有全部硬链接、路径白名单与保护状态均通过，计划才会放行。"}
              </span>
            </div>

            <div className="selected-resource-list">
              {selectedItems.map((item) => (
                <div key={item.id}>
                  <p>
                    <strong>{item.title}</strong>
                    <small>
                      {item.englishTitle} · {item.edition}
                    </small>
                  </p>
                  <b>{item.sizeLabel}</b>
                </div>
              ))}
            </div>

            <section className="plan-result" aria-live="polite">
              {planLoading && <p className="plan-loading">正在复核实时关系…</p>}
              {planError && (
                <div className="plan-blocked">
                  <strong>暂时无法生成计划</strong>
                  <span>{planError}</span>
                </div>
              )}
              {plan && (
                <>
                  <div
                    className={
                      plan.canExecute ? "plan-passed" : "plan-blocked"
                    }
                  >
                    <strong>
                      {plan.canExecute ? "安全预演通过" : "计划已被安全门禁拦截"}
                    </strong>
                    {plan.canExecute ? (
                      <span>
                        {[
                          plan.operationCounts.qbStop
                            ? `停止 ${plan.operationCounts.qbStop} 个 qB 任务`
                            : "",
                          plan.operationCounts.qbRemoveKeepFiles
                            ? `移除 ${plan.operationCounts.qbRemoveKeepFiles} 个 qB 任务`
                            : "",
                          plan.operationCounts.unlinkFiles
                            ? `精确解除 ${plan.operationCounts.unlinkFiles} 个文件入口`
                            : "",
                          plan.operationCounts.moviepilotIndexes
                            ? `清理 ${plan.operationCounts.moviepilotIndexes} 条 MoviePilot 媒体索引`
                            : "",
                        ]
                          .filter(Boolean)
                          .join("，") || "无可执行操作"}
                      </span>
                    ) : (
                      <span>
                        未生成可执行操作
                        {plan.operationCounts.qbStop ||
                        plan.operationCounts.qbRemoveKeepFiles ||
                        plan.operationCounts.unlinkFiles ||
                        plan.operationCounts.moviepilotIndexes
                          ? "；下列关联影响仅供复核，不会执行"
                          : ""}
                      </span>
                    )}
                  </div>

                  {!!plan.blocks.length && (
                    <ul className="plan-issues blocked">
                      {plan.blocks.map((issue) => (
                        <li key={issue.code}>{issue.message}</li>
                      ))}
                    </ul>
                  )}
                  {plan.resources.some((item) => item.missingFiles?.length) && (
                    <ul className="plan-issues blocked">
                      {plan.resources.flatMap((item) =>
                        (item.missingFiles || []).map((missing) => (
                          <li
                            key={`${item.id}-missing-${missing.episode || missing.name}-${missing.source}`}
                          >
                            缺失 {missing.episode ? `${missing.episode} · ` : ""}
                            {missing.name}（来源：{missing.source}
                            {missing.expectedSizeBytes
                              ? `，应有 ${formatBytes(missing.expectedSizeBytes)}`
                              : ""}
                            ）
                          </li>
                        )),
                      )}
                    </ul>
                  )}
                  {!!plan.warnings.length && (
                    <ul className="plan-issues warning">
                      {plan.warnings.map((issue) => (
                        <li key={issue.code}>{issue.message}</li>
                      ))}
                    </ul>
                  )}
                  {plan.requiresSiteAcknowledgement && (
                    <label className="site-risk-check">
                      <input
                        type="checkbox"
                        checked={acknowledgeSiteRisk}
                        onChange={(event) =>
                          toggleSiteRisk(event.target.checked)
                        }
                      />
                      <span />
                      我已确认这会影响私有站做种，并接受站点规则风险
                    </label>
                  )}
                  {plan.requiresMissingFileAcknowledgement &&
                    actionMode === "delete" && (
                      <label className="site-risk-check missing-file-check">
                        <input
                          type="checkbox"
                          checked={acknowledgeMissingFiles}
                          onChange={(event) =>
                            toggleMissingFileAcknowledgement(
                              event.target.checked,
                            )
                          }
                        />
                        <span />
                        我已确认缺失的必需视频文件不会被删除，只清理其余已核验任务、文件和媒体索引
                      </label>
                    )}
                  {planExpired && (
                    <div className="plan-blocked" role="alert">
                      <strong>安全预演已过期</strong>
                      <span>资源状态可能已经变化，请关闭后重新生成。</span>
                    </div>
                  )}
                </>
              )}
            </section>

            <div className="preview-lock">
              <span>盾</span>
              <p>
                <strong>
                  {executionEnabled
                    ? "执行功能已启用，需连续点击两次确认"
                    : "当前只生成真实计划，不会执行"}
                </strong>
                <small>
                  {plan?.canExecute
                    ? planExpired
                      ? "计划已过期，不能继续执行。"
                      : "第二次点击前会复核当前清单，执行器只回读所选资源。"
                    : "修复全部拦截项后才能进入执行确认。"}
                </small>
              </p>
            </div>

            {executeResult ? (
              <div className="execution-success" role="status">
                <strong>{plan?.modeLabel}已完成</strong>
                <span>
                  {executeResult.qbStopped
                    ? `已停止 ${executeResult.qbStopped} 个 qB 任务。`
                    : ""}
                  {executeResult.qbRemoved
                    ? `已退出 ${executeResult.qbRemoved} 个 qB 任务。`
                    : ""}
                  {executeResult.filesDeleted
                    ? `已删除 ${executeResult.filesDeleted} 个精确文件入口。`
                    : ""}
                  {executeResult.missingFilesAlreadyAbsent
                    ? `已核对 ${executeResult.missingFilesAlreadyAbsent} 个缺失入口仍不存在（不计释放量）。`
                    : ""}
                  {executeResult.moviepilotIndexesDeleted
                    ? `已清理 ${executeResult.moviepilotIndexesDeleted} 条 MoviePilot 媒体索引。`
                    : ""}
                  {executeResult.snapshotRefreshPending
                    ? plan?.mode === "delete"
                      ? " 已从当前列表移除；请刷新资源清单后继续操作。"
                      : " 操作已完成；请刷新资源清单后继续操作。"
                    : ""}
                </span>
                <button type="button" onClick={closePlan}>
                  完成
                </button>
              </div>
            ) : confirmationOpen ? (
              <section className="final-confirmation">
                <strong>请再次点击确认，执行前会复核当前清单并回读所选资源</strong>
                {executeError && (
                  <p className="execute-error" role="alert">
                    {executeError}
                  </p>
                )}
                <div>
                  <button
                    type="button"
                    disabled={executing}
                    onClick={() => {
                      setConfirmationOpen(false);
                      setExecuteError("");
                    }}
                  >
                    返回
                  </button>
                  <button
                    type="button"
                    className={
                      actionMode === "delete" ? "danger-execute" : ""
                    }
                    disabled={
                      executing || planExpired
                    }
                    onClick={() => void executePlan()}
                  >
                    {executing ? "正在定向复核…" : `确认${plan?.modeLabel}`}
                  </button>
                </div>
              </section>
            ) : (
              <button
                type="button"
                className="confirm-preview"
                disabled={
                  !executionEnabled || !plan?.canExecute || planExpired
                }
                onClick={() => setConfirmationOpen(true)}
              >
                {executionEnabled ? "进入最终确认" : "执行引擎尚未启用"}
              </button>
            )}
          </section>
        </div>
      )}
    </main>
  );
}
