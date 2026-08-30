import { importShared } from './__federation_fn_import-JrT3xvdd.js';
import Config, { _ as _export_sfc } from './__federation_expose_Config-B8QqIPN4.js';

const FILTER_GROUPS = [
  {
    id: 'type',
    label: '资源类型',
    multi: false,
    options: [
      { id: 'all', label: '全部资源' },
      { id: 'movie', label: '电影' },
      { id: 'tv', label: '电视剧' },
    ],
  },
  {
    id: 'library',
    label: '媒体库状态',
    multi: false,
    options: [
      { id: 'all', label: '全部' },
      { id: 'imported', label: '已入库' },
      { id: 'not-imported', label: '未入库' },
    ],
  },
  {
    id: 'seed',
    label: '做种约束',
    multi: false,
    options: [
      { id: 'all', label: '全部' },
      { id: 'hr', label: 'H&R 保护中', tone: 'warning' },
      { id: 'none', label: '无保护约束' },
    ],
  },
  {
    id: 'flags',
    label: '待处理 / 质量',
    multi: true,
    options: [
      { id: 'incomplete', label: '剧集不完整', tone: 'warning' },
      { id: 'name-pending', label: '名称待确认' },
    ],
  },
];

function createFilterState() {
  return { type: 'all', library: 'all', seed: 'all', flags: [] }
}

const ACTIONS = {
  pause: {
    title: '停止做种',
    detail: '保留 qB 任务和全部文件',
  },
  retire: {
    title: '退出做种',
    detail: '移除 qB 任务，媒体仍保留',
  },
  delete: {
    title: '完整删除',
    detail: '移除任务、媒体和全部链接',
  },
};

function unwrapResponse(response) {
  if (response && Object.prototype.hasOwnProperty.call(response, 'data') && response.success !== undefined) {
    return response.data
  }
  return response?.data ?? response
}

function createLatestPlanApi(api) {
  let generation = 0;
  let latestPlanResult = null;

  return {
    ...api,
    get(...args) {
      return api.get(...args)
    },
    post(path, body, ...args) {
      if (!String(path || '').endsWith('/plan')) {
        return api.post(path, body, ...args)
      }

      const requestGeneration = ++generation;
      let rawRequest;
      try {
        rawRequest = Promise.resolve(api.post(path, body, ...args));
      } catch (error) {
        rawRequest = Promise.reject(error);
      }

      const result = (async () => {
        try {
          const response = await rawRequest;
          if (requestGeneration !== generation && latestPlanResult) {
            return await latestPlanResult
          }
          return response
        } catch (error) {
          if (requestGeneration !== generation && latestPlanResult) {
            return await latestPlanResult
          }
          throw error
        }
      })();
      latestPlanResult = result;
      return result
    },
  }
}

function mediaType(item) {
  const type = String(item?.type || '').trim().toLowerCase();
  if (type === '电影' || type === 'movie') return 'movie'
  if (type === '电视剧' || type === 'tv' || type === 'series') return 'tv'

  // Keep older snapshots usable when the explicit type field is absent.
  const edition = String(item?.edition || '').trim().toLowerCase();
  if (edition === '电影' || edition.startsWith('电影 ·')) return 'movie'
  return ''
}

function isIncompleteTv(item) {
  return mediaType(item) === 'tv' && item?.episodeIncomplete === true
}

function matchesFilter(item, filter) {
  if (filter === 'all') return true
  if (filter === 'movie') return mediaType(item) === 'movie'
  if (filter === 'tv') return mediaType(item) === 'tv'
  if (filter === 'tv-incomplete') return isIncompleteTv(item)
  if (filter === 'library') return Boolean(item.library)
  if (filter === 'hr') return Boolean(item.hr || item.hrPending)
  if (filter === 'review') return item.protected !== true
  return item.metadataVerified === false
}

function matchesFlag(item, flag) {
  if (flag === 'incomplete') return isIncompleteTv(item)
  if (flag === 'name-pending') return item.metadataVerified === false
  return false
}

function matchesFilterState(item, filters = createFilterState()) {
  const state = filters || createFilterState();
  const type = state.type || 'all';
  const library = state.library || 'all';
  const seed = state.seed || 'all';
  const flags = Array.isArray(state.flags) ? state.flags : [];

  if (type !== 'all' && mediaType(item) !== type) return false
  if (library === 'imported' && !item.library) return false
  if (library === 'not-imported' && item.library) return false
  if (seed === 'hr' && !matchesFilter(item, 'hr')) return false
  if (seed === 'none' && !matchesFilter(item, 'review')) return false
  return flags.every(flag => matchesFlag(item, flag))
}

function legacyFilterState(filter) {
  const state = createFilterState();
  if (filter === 'movie' || filter === 'tv') state.type = filter;
  else if (filter === 'tv-incomplete') state.flags = ['incomplete'];
  else if (filter === 'library') state.library = 'imported';
  else if (filter === 'hr') state.seed = 'hr';
  else if (filter === 'review') state.seed = 'none';
  else if (filter === 'names') state.flags = ['name-pending'];
  return state
}

function filterOptionCount(resources, filters, groupId, optionId) {
  const state = {
    ...createFilterState(),
    ...(filters || {}),
    flags: Array.isArray(filters?.flags) ? [...filters.flags] : [],
  };
  if (groupId === 'type') state.type = optionId;
  if (groupId === 'library') state.library = optionId;
  if (groupId === 'seed') state.seed = optionId;
  if (groupId === 'flags') {
    state.flags = optionId === 'all'
      ? []
      : [...new Set([...state.flags, optionId])];
  }
  return (resources || []).filter(item => matchesFilterState(item, state)).length
}

function hasNoProtectionConstraint(item) {
  return item.protected !== true
}

function filterResources(resources, { filter, filters, search, safeOnly, descending }) {
  const query = String(search || '').trim().toLowerCase();
  const state = filters || legacyFilterState(filter);
  return [...(resources || [])]
    .filter(item => {
      const text = `${item.title || ''} ${item.englishTitle || ''} ${item.edition || ''} ${item.siteSummary || ''}`.toLowerCase();
      return matchesFilterState(item, state) &&
        (!safeOnly || hasNoProtectionConstraint(item)) &&
        (!query || text.includes(query))
    })
    .sort((left, right) => {
      const order = descending ? Number(right.size || 0) - Number(left.size || 0) : Number(left.size || 0) - Number(right.size || 0);
      return order || String(left.title || '').localeCompare(String(right.title || ''), 'zh-CN')
    })
}

function formatGiB(size) {
  const numeric = Number(size || 0);
  return numeric >= 1024 ? `${(numeric / 1024).toFixed(2)} TB` : `${numeric.toFixed(1)} GB`
}

function formatBytes(size) {
  return formatGiB(Number(size || 0) / 1024 ** 3)
}

function issueKey(issue, index) {
  return `${issue?.code || 'issue'}-${index}`
}

function refreshFeedback(elapsedSeconds, includeHitAndRun = true) {
  const elapsed = Math.max(0, Number(elapsedSeconds) || 0);
  if (elapsed < 5) return '正在读取 NAS 只读快照…'
  if (elapsed < 30) {
    return includeHitAndRun
      ? `正在核对媒体目录、qB 与 H&R（已等待 ${elapsed} 秒）`
      : `正在核对媒体目录与 qB（已等待 ${elapsed} 秒）`
  }
  return includeHitAndRun
    ? `远端 H&R 探测可能需要数分钟（已等待 ${elapsed} 秒），请保持页面打开。`
    : `远端资源核对可能需要数分钟（已等待 ${elapsed} 秒），请保持页面打开。`
}

const {createElementVNode:_createElementVNode,toDisplayString:_toDisplayString,openBlock:_openBlock,createElementBlock:_createElementBlock,createCommentVNode:_createCommentVNode,normalizeClass:_normalizeClass,vModelText:_vModelText,withDirectives:_withDirectives,vModelCheckbox:_vModelCheckbox,createTextVNode:_createTextVNode,renderList:_renderList,Fragment:_Fragment,unref:_unref,withModifiers:_withModifiers,createVNode:_createVNode,Teleport:_Teleport,createBlock:_createBlock} = await importShared('vue');


const _hoisted_1 = { class: "cleanup-app" };
const _hoisted_2 = {
  key: 0,
  class: "page-header"
};
const _hoisted_3 = { key: 0 };
const _hoisted_4 = { class: "toolbar" };
const _hoisted_5 = { class: "search" };
const _hoisted_6 = { class: "safe-toggle" };
const _hoisted_7 = ["disabled", "aria-label", "aria-busy", "title"];
const _hoisted_8 = {
  key: 0,
  class: "refresh-feedback",
  role: "status",
  "aria-live": "polite"
};
const _hoisted_9 = {
  key: 1,
  class: "notice critical stale-notice",
  role: "status"
};
const _hoisted_10 = {
  key: 2,
  class: "onboarding-card"
};
const _hoisted_11 = { key: 0 };
const _hoisted_12 = { key: 1 };
const _hoisted_13 = {
  key: 4,
  class: "notice warning hr-failure-notice",
  role: "status"
};
const _hoisted_14 = {
  class: "risk-overview",
  "aria-label": "清理台风险总览"
};
const _hoisted_15 = { class: "risk-overview-card" };
const _hoisted_16 = { class: "risk-overview-card" };
const _hoisted_17 = { class: "risk-overview-card risk-overview-warn" };
const _hoisted_18 = {
  class: "filter-panel",
  "aria-label": "资源筛选"
};
const _hoisted_19 = { class: "filter-label" };
const _hoisted_20 = { class: "filter-options" };
const _hoisted_21 = ["aria-pressed", "onClick"];
const _hoisted_22 = { class: "filter-footer" };
const _hoisted_23 = {
  class: "active-filter-chips",
  "aria-live": "polite"
};
const _hoisted_24 = {
  key: 0,
  class: "filter-caption"
};
const _hoisted_25 = ["onClick"];
const _hoisted_26 = { class: "filter-result-count" };
const _hoisted_27 = { class: "resource-card" };
const _hoisted_28 = { class: "table-head" };
const _hoisted_29 = {
  key: 0,
  class: "empty-state"
};
const _hoisted_30 = ["disabled", "onClick"];
const _hoisted_31 = { class: "resource-title" };
const _hoisted_32 = {
  class: "stack-cell library",
  "data-label": "媒体库"
};
const _hoisted_33 = { key: 0 };
const _hoisted_34 = {
  class: "seed-cell",
  "data-label": "做种与保护"
};
const _hoisted_35 = {
  key: 1,
  class: "stack-cell"
};
const _hoisted_36 = {
  class: "stack-cell size",
  "data-label": "实际占用"
};
const _hoisted_37 = { key: 0 };
const _hoisted_38 = {
  key: 1,
  class: "shared-hardlink-impact"
};
const _hoisted_39 = ["onClick"];
const _hoisted_40 = { key: 1 };
const _hoisted_41 = {
  key: 2,
  class: "empty-state"
};
const _hoisted_42 = {
  key: 0,
  class: "action-bar"
};
const _hoisted_43 = { class: "selected-count" };
const _hoisted_44 = { class: "action-buttons" };
const _hoisted_45 = ["disabled", "title", "onClick"];
const _hoisted_46 = {
  class: "modal plan-modal",
  role: "dialog",
  "aria-modal": "true"
};
const _hoisted_47 = ["disabled"];
const _hoisted_48 = { key: 0 };
const _hoisted_49 = { key: 1 };
const _hoisted_50 = { class: "plan-resources" };
const _hoisted_51 = {
  key: 0,
  class: "plan-state"
};
const _hoisted_52 = {
  key: 1,
  class: "plan-state blocked"
};
const _hoisted_53 = { key: 0 };
const _hoisted_54 = { key: 1 };
const _hoisted_55 = { key: 0 };
const _hoisted_56 = {
  key: 0,
  class: "issues blocked"
};
const _hoisted_57 = {
  key: 1,
  class: "issues blocked"
};
const _hoisted_58 = {
  key: 2,
  class: "issues warning"
};
const _hoisted_59 = {
  key: 3,
  class: "risk-check"
};
const _hoisted_60 = ["checked"];
const _hoisted_61 = {
  key: 4,
  class: "risk-check missing-file-check"
};
const _hoisted_62 = ["checked"];
const _hoisted_63 = {
  key: 5,
  class: "plan-state blocked"
};
const _hoisted_64 = { class: "safety-note" };
const _hoisted_65 = {
  key: 3,
  class: "plan-state blocked"
};
const _hoisted_66 = {
  key: 4,
  class: "execution-result"
};
const _hoisted_67 = { key: 0 };
const _hoisted_68 = {
  key: 5,
  class: "final-confirmation"
};
const _hoisted_69 = ["disabled"];
const _hoisted_70 = ["disabled"];
const _hoisted_71 = ["disabled"];
const _hoisted_72 = { class: "modal compact-modal" };
const _hoisted_73 = {
  key: 0,
  class: "empty-state"
};
const _hoisted_74 = {
  key: 1,
  class: "plan-state blocked"
};
const _hoisted_75 = { class: "modal compact-modal" };
const _hoisted_76 = {
  key: 0,
  class: "empty-state"
};
const _hoisted_77 = {
  key: 1,
  class: "plan-state blocked"
};
const _hoisted_78 = ["onClick"];
const _hoisted_79 = ["onClick"];
const _hoisted_80 = {
  key: 2,
  class: "recovery-confirm"
};
const _hoisted_81 = ["disabled"];
const _hoisted_82 = {
  class: "modal settings-modal",
  role: "dialog",
  "aria-modal": "true",
  "aria-labelledby": "storage-cleanup-settings-title"
};

const {computed,onMounted,onUnmounted,ref} = await importShared('vue');


const _sfc_main = {
  __name: 'AppPage',
  props: {
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'StorageCleanup' },
  hideTitle: { type: Boolean, default: false },
},
  setup(__props) {

const props = __props;

const emptySnapshot = {
  schemaVersion: 2,
  snapshotId: '',
  generatedAt: '',
  stats: {},
  resources: [],
};

const snapshot = ref(emptySnapshot);
const health = ref({});
const loading = ref(true);
const refreshing = ref(false);
const refreshElapsed = ref(0);
const error = ref('');
const search = ref('');
const filterState = ref(createFilterState());
const safeOnly = ref(false);
const descending = ref(true);
const selected = ref([]);

const planOpen = ref(false);
const planMode = ref(null);
const plan = ref(null);
const planLoading = ref(false);
const planError = ref('');
const acknowledgeSiteRisk = ref(false);
const acknowledgeMissingFiles = ref(false);
const finalConfirmation = ref(false);
const executing = ref(false);
const executeError = ref('');
const executeResult = ref(null);

const gapOpen = ref(false);
const gapLoading = ref(false);
const gaps = ref([]);
const gapError = ref('');

const recoveryOpen = ref(false);
const recoveryLoading = ref(false);
const recoveries = ref([]);
const recoveryError = ref('');
const recoveryTarget = ref(null);
const recoveryAction = ref(null);
const recoveryPhrase = ref('');
const recovering = ref(false);
const settingsOpen = ref(false);

const pluginBase = computed(() => `plugin/${props.pluginId || 'StorageCleanup'}`);
const resources = computed(() => snapshot.value.resources || []);
const visible = computed(() => filterResources(resources.value, {
  filters: filterState.value,
  search: search.value,
  safeOnly: safeOnly.value,
  descending: descending.value,
}));
const selectedItems = computed(() => resources.value.filter(item => selected.value.includes(item.id)));
const planMissingFiles = computed(() => (plan.value?.resources || []).flatMap(item => (
  (item.missingFiles || []).map(missing => ({
    ...missing,
    key: `${item.id}-missing-${missing.episode || missing.name}-${missing.source}`,
  }))
)));
const selectedSize = computed(() => selectedItems.value.reduce((total, item) => total + Number(item.size || 0), 0));
const executionEnabled = computed(() => Boolean(health.value.executionEnabled));
const snapshotFresh = computed(() => health.value.snapshotFresh !== false);
const inventoryCurrent = computed(() => health.value.inventoryCurrent !== false && snapshotFresh.value);
const onboardingRequired = computed(() => !loading.value && (
  !snapshot.value.snapshotId || health.value.configReady === false
));
const unresolvedTransactions = computed(() => Number(snapshot.value.stats?.unresolvedTransactions || 0));
const hitAndRunEnabled = computed(() => {
  if (typeof health.value.hitAndRunEnabled === 'boolean') {
    return health.value.hitAndRunEnabled
  }
  return Boolean(snapshot.value.snapshotId && snapshot.value.stats?.hrEnabled !== false)
});
const hrFailures = computed(() => {
  if (!hitAndRunEnabled.value) return []
  return Object.entries(snapshot.value.stats?.hrSources || {})
    .filter(([, source]) => source?.available !== true && source?.error)
    .map(([site, source]) => ({
      site: source?.taskLabel || site,
      error: source?.validated === true
        ? `${source.error}；该站已采取保护优先`
        : `${source.error}；首次验证未成功，H&R 尚未生效`,
    }))
});
const hrGap = computed(() => {
  if (!hitAndRunEnabled.value) return 0
  return Math.max(
    0,
    Number(
      snapshot.value.stats?.hrMissingQbTasks ??
      Number(snapshot.value.stats?.hrActiveTitles || 0) - Number(snapshot.value.stats?.hrMatchedQbTasks || 0),
    ),
  )
});
const hrUnassigned = computed(() => Number(
  hitAndRunEnabled.value
    ? (snapshot.value.stats?.hrMissingUnassigned ??
      snapshot.value.stats?.hrMissingUncovered ??
      0)
    : 0,
));
const riskSummary = computed(() => {
  const items = resources.value;
  return {
    total: items.length,
    imported: items.filter(item => item.library).length,
    notImported: items.filter(item => !item.library).length,
    protected: items.filter(item => item.protected).length,
    shared: items.filter(item => (item.sharedHardlinkResources?.length || 0) > 0).length,
    review: Number(snapshot.value.stats?.bilingualMissingResources || 0) + Number(snapshot.value.stats?.metadataUnverifiedResources || 0),
    qbTasks: Number(snapshot.value.stats?.qbTasks || 0),
    matchedQbTasks: Number(snapshot.value.stats?.matchedQbTasks || 0),
    unmatchedQbTasks: Number(snapshot.value.stats?.unmatchedQbTasks || 0),
  }
});
const snapshotAgeLabel = computed(() => formatSnapshotAge(snapshot.value.generatedAt));
const snapshotMaxAgeLabel = computed(() => formatSnapshotLimit(Number(health.value.snapshotMaxAgeSeconds || 3600)));
const filterGroups = computed(() => FILTER_GROUPS.map(group => ({
  ...group,
  options: group.options
    .filter(option => hitAndRunEnabled.value || option.id !== 'hr')
    .map(option => ({
    ...option,
    count: filterOptionCount(resources.value, filterState.value, group.id, option.id),
    })),
})));
const activeFilterChips = computed(() => {
  const chips = [];
  for (const group of filterGroups.value) {
    const selected = group.id === 'flags'
      ? filterState.value.flags
      : [filterState.value[group.id]];
    for (const option of group.options) {
      if (option.id !== 'all' && selected.includes(option.id)) {
        chips.push({ group: group.id, id: option.id, label: option.label });
      }
    }
  }
  return chips
});
const allVisibleSelected = computed(() => {
  const selectable = visible.value.filter(item => !item.protected);
  return selectable.length > 0 && selectable.every(item => selected.value.includes(item.id))
});
const currentAction = computed(() => planMode.value ? ACTIONS[planMode.value] : null);
const planExpired = computed(() => Boolean(plan.value && Date.parse(plan.value.expiresAt) <= Date.now()));
const allFiltersDefault = computed(() => activeFilterChips.value.length === 0);
const refreshMessage = computed(() => {
  if (!refreshing.value) return ''
  return refreshFeedback(refreshElapsed.value, hitAndRunEnabled.value)
});

let refreshTimer = null;

function stopRefreshTimer() {
  if (refreshTimer !== null) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

function startRefreshTimer() {
  stopRefreshTimer();
  refreshElapsed.value = 0;
  refreshTimer = setInterval(() => {
    refreshElapsed.value += 1;
  }, 1000);
}

function payloadError(payload, fallback) {
  return payload?.error?.message || fallback
}

function formatSnapshotAge(generatedAt) {
  const timestamp = Date.parse(generatedAt || '');
  if (!Number.isFinite(timestamp)) return '时间戳无效'
  const seconds = Math.floor((Date.now() - timestamp) / 1000);
  if (seconds < 0) return '时间戳晚于本机时钟'
  if (seconds < 60) return '刚刚更新'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前更新`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前更新`
  return `${Math.floor(seconds / 86400)} 天前更新`
}

function formatSnapshotLimit(seconds) {
  if (seconds >= 86400 && seconds % 86400 === 0) return `${seconds / 86400} 天`
  if (seconds >= 3600 && seconds % 3600 === 0) return `${seconds / 3600} 小时`
  return `${Math.max(1, Math.floor(seconds / 60))} 分钟`
}

function requestErrorMessage(error, fallback) {
  const response = error?.response || {};
  const payload = response.data || error?.data || {};
  const nested = payload?.error || {};
  if (nested.code === 'inventory_stale' || response.status === 409 || error?.status === 409) {
    health.value = { ...health.value, inventoryCurrent: false, snapshotFresh: false };
    return '资源清单已过期，请点击“刷新资源清单”后再操作。浏览器重新加载不会重新核对 NAS。'
  }
  return nested.message || payload.message || error?.message || fallback
}

const EXECUTION_STALE_CODES = new Set([
  'inventory_stale',
  'preflight_refresh_failed',
  'plan_rebuild_failed',
  'plan_not_cached',
]);

function executionErrorMessage(error, fallback) {
  const response = error?.response || {};
  const payload = response.data || error?.data || {};
  const nested = payload?.error || {};

  // Axios rejects non-2xx responses, so the control-service payload lives on
  // the error object instead of reaching unwrapResponse(). Preserve a
  // refreshed plan when the final preflight found changed task/file state.
  if (nested.plan) plan.value = nested.plan;
  if (EXECUTION_STALE_CODES.has(nested.code)) {
    health.value = { ...health.value, inventoryCurrent: false, snapshotFresh: false };
    selected.value = [];
    if (!nested.plan) plan.value = null;
  }

  if (nested.message || payload.message) return nested.message || payload.message
  if (response.status === 409 || error?.status === 409) {
    return '执行请求已被安全门禁拒绝，请重新生成并确认计划。'
  }
  return error?.message || fallback
}

async function get(path) {
  return unwrapResponse(await props.api.get(`${pluginBase.value}${path}`))
}

async function post(path, body) {
  return unwrapResponse(await props.api.post(`${pluginBase.value}${path}`, body))
}

function acceptSnapshot(next) {
  if (!next || next.schemaVersion !== 2 || !next.snapshotId || !Array.isArray(next.resources)) {
    throw new Error('资源快照格式不受支持。')
  }
  snapshot.value = next;
  if (!hitAndRunEnabled.value && filterState.value.seed === 'hr') {
    filterState.value = createFilterState();
  }
  const available = new Set(next.resources.map(item => item.id));
  selected.value = selected.value.filter(id => available.has(id));
}

async function loadStatus() {
  loading.value = true;
  error.value = '';
  try {
    const payload = await get('/status');
    if (!payload?.ok || !payload.snapshot) throw new Error(payloadError(payload, '无法读取清理台状态。'))
    health.value = payload.health || {};
    acceptSnapshot(payload.snapshot);
    if (
      typeof health.value.hitAndRunEnabled === 'boolean'
      && payload.snapshot.stats?.hrEnabled !== health.value.hitAndRunEnabled
    ) {
      health.value = { ...health.value, inventoryCurrent: false };
      void refreshSnapshot();
    }
    if (!inventoryCurrent.value) {
      selected.value = [];
    }
  } catch (err) {
    error.value = err?.message || '无法读取清理台状态。';
  } finally {
    loading.value = false;
  }
}

async function refreshSnapshot() {
  if (refreshing.value) return
  refreshing.value = true;
  startRefreshTimer();
  error.value = '';
  try {
    const payload = await post('/refresh', {});
    if (!payload?.ok || !payload.snapshot) throw new Error(payloadError(payload, '刷新失败。'))
    acceptSnapshot(payload.snapshot);
    health.value = { ...health.value, inventoryCurrent: true, snapshotFresh: true };
  } catch (err) {
    error.value = err?.message || '刷新失败，继续显示上次快照。';
    health.value = { ...health.value, inventoryCurrent: false, snapshotFresh: false };
  } finally {
    stopRefreshTimer();
    refreshing.value = false;
  }
}

function toggle(item) {
  if (item.protected) return
  selected.value = selected.value.includes(item.id)
    ? selected.value.filter(id => id !== item.id)
    : [...selected.value, item.id];
}

function selectSharedResources(item) {
  const relatedIds = (item.sharedHardlinkResources || [])
    .map(related => related.id)
    .filter(id => resources.value.some(candidate => candidate.id === id && !candidate.protected));
  if (!relatedIds.length) return
  selected.value = [...new Set([...selected.value, ...relatedIds])];
}

function toggleVisible() {
  const ids = visible.value.filter(item => !item.protected).map(item => item.id);
  selected.value = allVisibleSelected.value
    ? selected.value.filter(id => !ids.includes(id))
    : [...new Set([...selected.value, ...ids])];
}

function isFilterActive(group, option) {
  if (group.id === 'flags') return filterState.value.flags.includes(option.id)
  return filterState.value[group.id] === option.id
}

function selectFilter(group, option) {
  if (group.id === 'flags') {
    filterState.value = {
      ...filterState.value,
      flags: filterState.value.flags.includes(option.id)
        ? filterState.value.flags.filter(id => id !== option.id)
        : [...filterState.value.flags, option.id],
    };
    return
  }
  filterState.value = { ...filterState.value, [group.id]: option.id };
}

function clearFilters() {
  filterState.value = createFilterState();
}

function clearFilterChip(chip) {
  if (chip.group === 'flags') {
    filterState.value = {
      ...filterState.value,
      flags: filterState.value.flags.filter(id => id !== chip.id),
    };
    return
  }
  filterState.value = { ...filterState.value, [chip.group]: 'all' };
}

async function requestPlan(mode, acknowledged = false, missingAcknowledged = false) {
  planLoading.value = true;
  planError.value = '';
  executeError.value = '';
  plan.value = null;
  finalConfirmation.value = false;
  if (!inventoryCurrent.value) {
    planError.value = '资源清单已过期，请点击“刷新资源清单”后再操作。浏览器重新加载不会重新核对 NAS。';
    planLoading.value = false;
    return
  }
  try {
    const payload = await post('/plan', {
      snapshotId: snapshot.value.snapshotId,
      resourceIds: selected.value,
      mode,
      acknowledgeSiteRisk: acknowledged,
      acknowledgeMissingFiles: missingAcknowledged,
    });
    if (!payload?.ok || !payload.plan) throw new Error(payloadError(payload, '无法生成执行计划。'))
    plan.value = payload.plan;
  } catch (err) {
    planError.value = requestErrorMessage(err, '无法生成执行计划。');
  } finally {
    planLoading.value = false;
  }
}

function openPlan(mode) {
  planMode.value = mode;
  planOpen.value = true;
  acknowledgeSiteRisk.value = false;
  acknowledgeMissingFiles.value = false;
  executeResult.value = null;
  executeError.value = '';
  requestPlan(mode, false, false);
}

function closePlan() {
  if (executing.value) return
  planOpen.value = false;
  planMode.value = null;
  plan.value = null;
  planError.value = '';
  acknowledgeMissingFiles.value = false;
  finalConfirmation.value = false;
  executeResult.value = null;
}

async function setSiteRisk(value) {
  acknowledgeSiteRisk.value = value;
  await requestPlan(planMode.value, value, acknowledgeMissingFiles.value);
}

async function setMissingFileAcknowledgement(value) {
  acknowledgeMissingFiles.value = value;
  finalConfirmation.value = false;
  executeError.value = '';
  await requestPlan(planMode.value, acknowledgeSiteRisk.value, value);
}

async function executePlan() {
  if (!plan.value || planExpired.value || executing.value || !inventoryCurrent.value) return
  executing.value = true;
  executeError.value = '';
  try {
    const payload = await post('/execute', {
      planId: plan.value.planId,
      confirmPhrase: plan.value.confirmPhrase,
    });
    if (!payload?.ok || !payload.result) {
      const requestFailure = new Error(payloadError(payload, '执行失败。'));
      requestFailure.data = payload;
      throw requestFailure
    }
    executeResult.value = payload.result;
    selected.value = [];
    if (plan.value?.mode === 'delete') {
      const deletedIds = new Set(plan.value.resources.map(item => item.id));
      snapshot.value = {
        ...snapshot.value,
        resources: snapshot.value.resources.filter(item => !deletedIds.has(item.id)),
      };
    }
    if (payload.result.snapshotRefreshPending) {
      health.value = { ...health.value, inventoryCurrent: false };
    } else {
      const latest = await get('/snapshot');
      if (latest?.snapshot) acceptSnapshot(latest.snapshot);
    }
  } catch (err) {
    executeError.value = executionErrorMessage(err, '执行失败。');
    finalConfirmation.value = false;
  } finally {
    executing.value = false;
  }
}

async function loadGaps() {
  gapOpen.value = true;
  gapLoading.value = true;
  gapError.value = '';
  try {
    const payload = await get('/protection-gaps');
    if (!payload?.ok) throw new Error(payloadError(payload, '无法读取 H&R 缺口。'))
    gaps.value = payload.gaps || [];
  } catch (err) {
    gapError.value = err?.message || '无法读取 H&R 缺口。';
  } finally {
    gapLoading.value = false;
  }
}

async function loadRecoveries() {
  recoveryOpen.value = true;
  recoveryLoading.value = true;
  recoveryError.value = '';
  recoveryTarget.value = null;
  try {
    const payload = await get('/recovery');
    if (!payload?.ok) throw new Error(payloadError(payload, '无法读取恢复状态。'))
    recoveries.value = payload.recoveries || [];
  } catch (err) {
    recoveryError.value = err?.message || '无法读取恢复状态。';
  } finally {
    recoveryLoading.value = false;
  }
}

function chooseRecovery(item, action) {
  recoveryTarget.value = item;
  recoveryAction.value = action;
  recoveryPhrase.value = '';
  recoveryError.value = '';
}

async function runRecovery() {
  if (!recoveryTarget.value || !recoveryAction.value || recovering.value) return
  recovering.value = true;
  recoveryError.value = '';
  try {
    const payload = await post('/recovery', {
      planId: recoveryTarget.value.planId,
      action: recoveryAction.value,
      confirmPhrase: recoveryPhrase.value,
    });
    if (!payload?.ok) throw new Error(payloadError(payload, '恢复操作失败。'))
    await refreshSnapshot();
    await loadRecoveries();
  } catch (err) {
    recoveryError.value = err?.message || '恢复操作失败。';
  } finally {
    recovering.value = false;
  }
}

function recoveryExpectedPhrase() {
  if (!recoveryTarget.value || !recoveryAction.value) return ''
  return recoveryAction.value === 'rollback'
    ? recoveryTarget.value.rollbackPhrase
    : recoveryTarget.value.finalizePhrase
}

function openSettings() {
  settingsOpen.value = true;
}

function closeSettings() {
  settingsOpen.value = false;
}

function handleConfigSaved(config) {
  health.value = {
    ...health.value,
    hitAndRunEnabled: config?.hit_and_run_enabled === true,
    inventoryCurrent: false,
    snapshotFresh: false,
  };
  selected.value = [];
  filterState.value = createFilterState();
  void refreshSnapshot();
}

onMounted(loadStatus);
onUnmounted(stopRefreshTimer);

return (_ctx, _cache) => {
  return (_openBlock(), _createElementBlock("main", _hoisted_1, [
    (!__props.hideTitle)
      ? (_openBlock(), _createElementBlock("header", _hoisted_2, [
          _cache[13] || (_cache[13] = _createElementVNode("div", null, [
            _createElementVNode("p", { class: "eyebrow" }, "安全清理台"),
            _createElementVNode("h1", null, "存储清理"),
            _createElementVNode("span", null, "一部电影一行，一部剧一行；先看清影响，再选择清理等级。")
          ], -1)),
          _createElementVNode("div", {
            class: _normalizeClass(['status-card', { danger: error.value || !inventoryCurrent.value }])
          }, [
            _createElementVNode("i", null, _toDisplayString(error.value || !inventoryCurrent.value ? '!' : '✓'), 1),
            _createElementVNode("p", null, [
              _createElementVNode("strong", null, _toDisplayString(error.value || (!inventoryCurrent.value ? '资源清单待刷新' : executionEnabled.value ? '执行链路已连接' : '只读模式')), 1),
              (snapshot.value.generatedAt)
                ? (_openBlock(), _createElementBlock("span", _hoisted_3, _toDisplayString(snapshotAgeLabel.value) + " · " + _toDisplayString(snapshot.value.generatedAt.slice(5, 16).replace('T', ' ')), 1))
                : _createCommentVNode("", true)
            ])
          ], 2)
        ]))
      : _createCommentVNode("", true),
    _createElementVNode("section", _hoisted_4, [
      _createElementVNode("label", _hoisted_5, [
        _cache[14] || (_cache[14] = _createElementVNode("span", null, "⌕", -1)),
        _withDirectives(_createElementVNode("input", {
          "onUpdate:modelValue": _cache[0] || (_cache[0] = $event => ((search).value = $event)),
          "aria-label": "搜索资源",
          placeholder: "搜索电影、剧集、季度或站点"
        }, null, 512), [
          [_vModelText, search.value]
        ])
      ]),
      _createElementVNode("label", _hoisted_6, [
        _withDirectives(_createElementVNode("input", {
          "onUpdate:modelValue": _cache[1] || (_cache[1] = $event => ((safeOnly).value = $event)),
          type: "checkbox"
        }, null, 512), [
          [_vModelCheckbox, safeOnly.value]
        ]),
        _cache[15] || (_cache[15] = _createElementVNode("span", null, null, -1)),
        _cache[16] || (_cache[16] = _createTextVNode(" 仅看无保护约束 ", -1))
      ]),
      _createElementVNode("button", {
        class: "soft-button",
        type: "button",
        onClick: _cache[2] || (_cache[2] = $event => (descending.value = !descending.value))
      }, " 实际占用 " + _toDisplayString(descending.value ? '↓' : '↑'), 1),
      _createElementVNode("button", {
        class: "soft-button settings-button",
        type: "button",
        "aria-label": "打开存储清理设置",
        onClick: openSettings
      }, " 设置 "),
      _createElementVNode("button", {
        class: "icon-button",
        type: "button",
        disabled: refreshing.value,
        "aria-label": refreshing.value ? '正在刷新资源清单' : '刷新资源清单',
        "aria-busy": refreshing.value,
        title: refreshMessage.value || '刷新资源清单',
        onClick: refreshSnapshot
      }, _toDisplayString(refreshing.value ? '…' : '↻'), 9, _hoisted_7),
      (refreshing.value)
        ? (_openBlock(), _createElementBlock("p", _hoisted_8, _toDisplayString(refreshMessage.value), 1))
        : _createCommentVNode("", true)
    ]),
    (!inventoryCurrent.value && !refreshing.value)
      ? (_openBlock(), _createElementBlock("div", _hoisted_9, [
          _cache[17] || (_cache[17] = _createElementVNode("i", null, "!", -1)),
          _cache[18] || (_cache[18] = _createElementVNode("p", null, [
            _createElementVNode("strong", null, "资源清单待刷新"),
            _createElementVNode("span", null, "浏览器重新加载只重载页面，不会重新核对 NAS；刷新完成前已锁定清理动作。")
          ], -1)),
          _createElementVNode("button", {
            type: "button",
            onClick: refreshSnapshot
          }, "刷新资源清单")
        ]))
      : _createCommentVNode("", true),
    (onboardingRequired.value)
      ? (_openBlock(), _createElementBlock("section", _hoisted_10, [
          _createElementVNode("div", null, [
            _createElementVNode("strong", null, _toDisplayString(health.value.configReady === false ? '清理台还没有完成配置' : '还没有连接到清理后台'), 1),
            (error.value)
              ? (_openBlock(), _createElementBlock("span", _hoisted_11, _toDisplayString(error.value), 1))
              : (_openBlock(), _createElementBlock("span", _hoisted_12, "请由 NAS 管理员先部署 PiNAS 清理台，并完成只读路径探测；插件不会自动登录或配置 NAS。"))
          ]),
          _createElementVNode("button", {
            type: "button",
            onClick: openSettings
          }, "打开设置")
        ]))
      : _createCommentVNode("", true),
    (unresolvedTransactions.value)
      ? (_openBlock(), _createElementBlock("button", {
          key: 3,
          class: "notice critical",
          type: "button",
          onClick: loadRecoveries
        }, [
          _cache[20] || (_cache[20] = _createElementVNode("i", null, "!", -1)),
          _createElementVNode("p", null, [
            _createElementVNode("strong", null, _toDisplayString(unresolvedTransactions.value) + " 个未完成清理事务", 1),
            _cache[19] || (_cache[19] = _createElementVNode("span", null, "新操作已锁定；请先核对并恢复原事务。", -1))
          ]),
          _cache[21] || (_cache[21] = _createElementVNode("b", null, "查看恢复状态", -1))
        ]))
      : _createCommentVNode("", true),
    (hrFailures.value.length)
      ? (_openBlock(), _createElementBlock("section", _hoisted_13, [
          _cache[22] || (_cache[22] = _createElementVNode("i", null, "H", -1)),
          _createElementVNode("p", null, [
            _createElementVNode("strong", null, _toDisplayString(hrFailures.value.length) + " 个 H&R 站点后台查询失败", 1),
            _createElementVNode("span", null, _toDisplayString(hrFailures.value.map(item => `${item.site}：${item.error}`).join('；')), 1)
          ])
        ]))
      : _createCommentVNode("", true),
    (!unresolvedTransactions.value && !hrFailures.value.length && hrGap.value)
      ? (_openBlock(), _createElementBlock("button", {
          key: 5,
          class: _normalizeClass(['notice', { warning: hrUnassigned.value }]),
          type: "button",
          onClick: loadGaps
        }, [
          _cache[23] || (_cache[23] = _createElementVNode("i", null, "H", -1)),
          _createElementVNode("p", null, [
            _createElementVNode("strong", null, _toDisplayString(hrGap.value) + " 个 H&R 任务尚未恢复完成", 1),
            _createElementVNode("span", null, _toDisplayString(hrUnassigned.value
            ? `${hrUnassigned.value} 个未精确关联媒体；不会锁定无关资源。`
            : '缺失任务只锁定精确关联资源，其他资源可独立清理。'), 1)
          ]),
          _cache[24] || (_cache[24] = _createElementVNode("b", null, "查看明细", -1))
        ], 2))
      : _createCommentVNode("", true),
    _createElementVNode("section", _hoisted_14, [
      _createElementVNode("article", _hoisted_15, [
        _cache[25] || (_cache[25] = _createElementVNode("span", null, "资源总览", -1)),
        _createElementVNode("strong", null, _toDisplayString(riskSummary.value.total), 1),
        _createElementVNode("small", null, "已入库 " + _toDisplayString(riskSummary.value.imported) + " · 未入库 " + _toDisplayString(riskSummary.value.notImported), 1)
      ]),
      _createElementVNode("article", _hoisted_16, [
        _cache[26] || (_cache[26] = _createElementVNode("span", null, "qB 任务", -1)),
        _createElementVNode("strong", null, _toDisplayString(riskSummary.value.qbTasks), 1),
        _createElementVNode("small", null, "已关联 " + _toDisplayString(riskSummary.value.matchedQbTasks) + " · 未关联 " + _toDisplayString(riskSummary.value.unmatchedQbTasks), 1)
      ]),
      _createElementVNode("article", _hoisted_17, [
        _cache[27] || (_cache[27] = _createElementVNode("span", null, "保护 / 复核", -1)),
        _createElementVNode("strong", null, _toDisplayString(riskSummary.value.protected), 1),
        _createElementVNode("small", null, "共享硬链接 " + _toDisplayString(riskSummary.value.shared) + " · 名称/字幕待复核 " + _toDisplayString(riskSummary.value.review), 1)
      ]),
      _createElementVNode("article", {
        class: _normalizeClass(['risk-overview-card', { 'risk-overview-danger': !snapshotFresh.value }])
      }, [
        _cache[28] || (_cache[28] = _createElementVNode("span", null, "快照状态", -1)),
        _createElementVNode("strong", null, _toDisplayString(snapshotFresh.value ? '新鲜' : '待刷新'), 1),
        _createElementVNode("small", null, _toDisplayString(snapshotAgeLabel.value) + " · 有效期 " + _toDisplayString(snapshotMaxAgeLabel.value), 1)
      ], 2)
    ]),
    _createElementVNode("nav", _hoisted_18, [
      (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(filterGroups.value, (group) => {
        return (_openBlock(), _createElementBlock("div", {
          key: group.id,
          class: "filter-row"
        }, [
          _createElementVNode("div", _hoisted_19, [
            _createTextVNode(_toDisplayString(group.label) + " ", 1),
            _createElementVNode("small", null, _toDisplayString(group.multi ? '可多选' : '单选'), 1)
          ]),
          _createElementVNode("div", _hoisted_20, [
            (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(group.options, (option) => {
              return (_openBlock(), _createElementBlock("button", {
                key: option.id,
                class: _normalizeClass(['filter-option', { active: isFilterActive(group, option) }, { warning: option.tone === 'warning' }]),
                "aria-pressed": isFilterActive(group, option),
                type: "button",
                onClick: $event => (selectFilter(group, option))
              }, [
                _createTextVNode(_toDisplayString(option.label) + " ", 1),
                _createElementVNode("span", null, _toDisplayString(option.count), 1)
              ], 10, _hoisted_21))
            }), 128))
          ])
        ]))
      }), 128)),
      _createElementVNode("div", _hoisted_22, [
        _createElementVNode("div", _hoisted_23, [
          (allFiltersDefault.value)
            ? (_openBlock(), _createElementBlock("span", _hoisted_24, "当前筛选：全部资源"))
            : (_openBlock(), _createElementBlock(_Fragment, { key: 1 }, [
                _cache[29] || (_cache[29] = _createElementVNode("span", { class: "filter-caption" }, "当前筛选", -1)),
                (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(activeFilterChips.value, (chip) => {
                  return (_openBlock(), _createElementBlock("button", {
                    key: `${chip.group}-${chip.id}`,
                    class: "active-filter-chip",
                    type: "button",
                    onClick: $event => (clearFilterChip(chip))
                  }, _toDisplayString(chip.label) + " × ", 9, _hoisted_25))
                }), 128))
              ], 64))
        ]),
        _createElementVNode("div", _hoisted_26, [
          _createElementVNode("strong", null, _toDisplayString(visible.value.length), 1),
          _cache[30] || (_cache[30] = _createTextVNode(" 条结果 ", -1)),
          (!allFiltersDefault.value)
            ? (_openBlock(), _createElementBlock("button", {
                key: 0,
                type: "button",
                onClick: clearFilters
              }, "清除筛选"))
            : _createCommentVNode("", true)
        ])
      ]),
      _cache[31] || (_cache[31] = _createElementVNode("p", { class: "filter-help" }, "同组条件单选；不同组条件按 AND 组合。待处理 / 质量标签可以叠加；无保护约束不等于可直接删除，仍需通过真实预演。", -1))
    ]),
    _createElementVNode("section", _hoisted_27, [
      _createElementVNode("div", _hoisted_28, [
        _createElementVNode("button", {
          class: "select-all",
          type: "button",
          onClick: toggleVisible
        }, _toDisplayString(allVisibleSelected.value ? '✓' : ''), 1),
        _cache[32] || (_cache[32] = _createElementVNode("span", null, "资源", -1)),
        _cache[33] || (_cache[33] = _createElementVNode("span", null, "媒体库", -1)),
        _cache[34] || (_cache[34] = _createElementVNode("span", null, "做种与保护", -1)),
        _cache[35] || (_cache[35] = _createElementVNode("span", null, "实际占用", -1)),
        _cache[36] || (_cache[36] = _createElementVNode("span", null, "完整删除影响", -1))
      ]),
      (loading.value)
        ? (_openBlock(), _createElementBlock("div", _hoisted_29, "正在读取真实资源关系…"))
        : (_openBlock(true), _createElementBlock(_Fragment, { key: 1 }, _renderList(visible.value, (item) => {
            return (_openBlock(), _createElementBlock("article", {
              key: item.id,
              class: _normalizeClass(['resource-row', { selected: selected.value.includes(item.id) }])
            }, [
              _createElementVNode("button", {
                class: _normalizeClass(['row-check', { locked: item.protected }]),
                type: "button",
                disabled: item.protected,
                onClick: $event => (toggle(item))
              }, _toDisplayString(item.protected ? '锁' : selected.value.includes(item.id) ? '✓' : ''), 11, _hoisted_30),
              _createElementVNode("div", _hoisted_31, [
                _createElementVNode("strong", null, _toDisplayString(item.title), 1),
                _createElementVNode("b", null, _toDisplayString(item.englishTitle), 1),
                _createElementVNode("span", null, _toDisplayString([item.type, item.year, item.edition].filter(Boolean).join(' · ')), 1)
              ]),
              _createElementVNode("div", _hoisted_32, [
                _createElementVNode("strong", null, _toDisplayString(item.librarySummary), 1),
                _createElementVNode("span", null, _toDisplayString(item.libraryDetail), 1),
                (item.episodeStatus === 'incomplete')
                  ? (_openBlock(), _createElementBlock("span", _hoisted_33, [
                      _createTextVNode(" 缺 " + _toDisplayString(item.episodeMissing) + " 集 ", 1),
                      (item.episodeMissingEpisodes?.length)
                        ? (_openBlock(), _createElementBlock(_Fragment, { key: 0 }, [
                            _createTextVNode(" （" + _toDisplayString(item.episodeMissingEpisodes.join('、')) + "） ", 1)
                          ], 64))
                        : _createCommentVNode("", true),
                      _createTextVNode(" · 已有 " + _toDisplayString(item.episodeActual) + " ", 1),
                      (item.episodeExpected)
                        ? (_openBlock(), _createElementBlock(_Fragment, { key: 1 }, [
                            _createTextVNode(" / 应有 " + _toDisplayString(item.episodeExpected), 1)
                          ], 64))
                        : (_openBlock(), _createElementBlock(_Fragment, { key: 2 }, [
                            _createTextVNode("（集号存在缺口，期望集数未知）")
                          ], 64))
                    ]))
                  : _createCommentVNode("", true)
              ]),
              _createElementVNode("div", _hoisted_34, [
                (item.seedTasks?.length)
                  ? (_openBlock(true), _createElementBlock(_Fragment, { key: 0 }, _renderList(item.seedTasks, (task, index) => {
                      return (_openBlock(), _createElementBlock("div", {
                        key: `${task.site}-${task.scope}-${index}`,
                        class: _normalizeClass(['seed-task', task.tone])
                      }, [
                        _createElementVNode("i", null, _toDisplayString(task.status), 1),
                        _createElementVNode("strong", null, _toDisplayString(task.site), 1),
                        _createElementVNode("span", null, _toDisplayString(task.scope) + _toDisplayString(task.count > 1 ? ` · ${task.count} 个任务` : ''), 1)
                      ], 2))
                    }), 128))
                  : (_openBlock(), _createElementBlock("div", _hoisted_35, [
                      _createElementVNode("strong", null, _toDisplayString(item.qbSummary), 1),
                      _createElementVNode("span", null, _toDisplayString(item.siteSummary), 1)
                    ]))
              ]),
              _createElementVNode("div", _hoisted_36, [
                _createElementVNode("strong", null, _toDisplayString(item.sizeLabel), 1),
                _createElementVNode("span", null, _toDisplayString(item.reclaimLabel), 1)
              ]),
              _createElementVNode("div", {
                class: _normalizeClass(['impact', { danger: item.protected }]),
                "data-label": "完整删除影响"
              }, [
                _createElementVNode("strong", null, _toDisplayString(item.impactTitle), 1),
                (item.protected && item.lockReason)
                  ? (_openBlock(), _createElementBlock("span", _hoisted_37, "锁定原因：" + _toDisplayString(item.lockReason), 1))
                  : _createCommentVNode("", true),
                _createElementVNode("span", null, _toDisplayString(item.impactDetail), 1),
                (item.sharedHardlinkResources?.length)
                  ? (_openBlock(), _createElementBlock("div", _hoisted_38, [
                      _cache[37] || (_cache[37] = _createElementVNode("strong", null, "共享硬链接影响", -1)),
                      _createElementVNode("span", null, " 与 " + _toDisplayString(item.sharedHardlinkResources.slice(0, 3).map(related => `${related.title}${related.edition ? `（${related.edition}）` : ''}${related.protected ? ' · 锁定' : ''}`).join('、')) + " " + _toDisplayString(item.sharedHardlinkResources.length > 3 ? `等 ${item.sharedHardlinkResources.length} 项` : '') + " 共用文件；完整删除需同时纳入并重新预演。 ", 1),
                      (item.sharedHardlinkResources.some(related => resources.value.some(candidate => candidate.id === related.id && !candidate.protected)))
                        ? (_openBlock(), _createElementBlock("button", {
                            key: 0,
                            type: "button",
                            class: "shared-hardlink-button",
                            onClick: $event => (selectSharedResources(item))
                          }, " 加入可选关联资源 ", 8, _hoisted_39))
                        : (_openBlock(), _createElementBlock("span", _hoisted_40, "关联资源含锁定项，不能单独清理。"))
                    ]))
                  : _createCommentVNode("", true)
              ], 2)
            ], 2))
          }), 128)),
      (!loading.value && !visible.value.length)
        ? (_openBlock(), _createElementBlock("div", _hoisted_41, " 没有符合条件的资源，请取消筛选或更换关键词。 "))
        : _createCommentVNode("", true)
    ]),
    (_openBlock(), _createBlock(_Teleport, { to: "body" }, [
      (selected.value.length)
        ? (_openBlock(), _createElementBlock("aside", _hoisted_42, [
            _createElementVNode("div", _hoisted_43, _toDisplayString(selected.value.length), 1),
            _createElementVNode("p", null, [
              _cache[38] || (_cache[38] = _createElementVNode("strong", null, "已加入清理计划", -1)),
              _createElementVNode("span", null, "完整删除上限 " + _toDisplayString(_unref(formatGiB)(selectedSize.value)), 1)
            ]),
            _createElementVNode("button", {
              class: "clear-button",
              type: "button",
              onClick: _cache[3] || (_cache[3] = $event => (selected.value = []))
            }, "清空"),
            _createElementVNode("div", _hoisted_44, [
              (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(_unref(ACTIONS), (action, mode) => {
                return (_openBlock(), _createElementBlock("button", {
                  key: mode,
                  class: _normalizeClass(['action-level', { delete: mode === 'delete' }]),
                  disabled: !inventoryCurrent.value || refreshing.value,
                  title: !inventoryCurrent.value ? '请先刷新资源清单' : action.detail,
                  type: "button",
                  onClick: $event => (openPlan(mode))
                }, [
                  _createElementVNode("strong", null, _toDisplayString(action.title), 1),
                  _createElementVNode("span", null, _toDisplayString(action.detail), 1)
                ], 10, _hoisted_45))
              }), 128))
            ])
          ]))
        : _createCommentVNode("", true),
      (planOpen.value)
        ? (_openBlock(), _createElementBlock("div", {
            key: 1,
            class: "modal-backdrop",
            onClick: _withModifiers(closePlan, ["self"])
          }, [
            _createElementVNode("section", _hoisted_46, [
              _createElementVNode("header", null, [
                _createElementVNode("div", null, [
                  _cache[39] || (_cache[39] = _createElementVNode("span", null, "清理等级 · 真实预演", -1)),
                  _createElementVNode("h2", null, _toDisplayString(currentAction.value?.title), 1)
                ]),
                _createElementVNode("button", {
                  type: "button",
                  disabled: executing.value,
                  onClick: closePlan
                }, "×", 8, _hoisted_47)
              ]),
              _createElementVNode("div", {
                class: _normalizeClass(['mode-summary', planMode.value])
              }, [
                (plan.value && planMode.value === 'delete')
                  ? (_openBlock(), _createElementBlock("strong", _hoisted_48, _toDisplayString(plan.value.canExecute
              ? `已核算可释放${plan.value.acknowledgeMissingFiles ? '（不含已确认缺失入口）' : ''} ${_unref(formatBytes)(plan.value.estimatedReclaimBytes)}`
              : '安全可释放暂不可核算'), 1))
                  : (_openBlock(), _createElementBlock("strong", _hoisted_49, _toDisplayString(currentAction.value?.detail), 1)),
                _createElementVNode("span", null, _toDisplayString(planMode.value === 'pause'
              ? '只改变 qB 运行状态，不删除任务或文件。'
              : planMode.value === 'retire'
                ? '移除 qB 任务但保留文件，媒体库继续可播放。'
                : '仅当全部路径、硬链接、H&R 与任务状态通过校验才会放行。'), 1)
              ], 2),
              _createElementVNode("div", _hoisted_50, [
                (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(selectedItems.value, (item) => {
                  return (_openBlock(), _createElementBlock("div", {
                    key: item.id
                  }, [
                    _createElementVNode("p", null, [
                      _createElementVNode("strong", null, _toDisplayString(item.title), 1),
                      _createElementVNode("span", null, _toDisplayString(item.englishTitle) + " · " + _toDisplayString(item.edition), 1)
                    ]),
                    _createElementVNode("b", null, _toDisplayString(item.sizeLabel), 1)
                  ]))
                }), 128))
              ]),
              (planLoading.value)
                ? (_openBlock(), _createElementBlock("div", _hoisted_51, "正在刷新 NAS 状态并复核关系…"))
                : (planError.value)
                  ? (_openBlock(), _createElementBlock("div", _hoisted_52, [
                      _cache[40] || (_cache[40] = _createElementVNode("strong", null, "无法生成计划", -1)),
                      _createElementVNode("span", null, _toDisplayString(planError.value), 1)
                    ]))
                  : (plan.value)
                    ? (_openBlock(), _createElementBlock(_Fragment, { key: 2 }, [
                        _createElementVNode("div", {
                          class: _normalizeClass(['plan-state', plan.value.canExecute ? 'passed' : 'blocked'])
                        }, [
                          _createElementVNode("strong", null, _toDisplayString(plan.value.canExecute ? '安全预演通过' : '计划已被安全门禁拦截'), 1),
                          (plan.value.canExecute)
                            ? (_openBlock(), _createElementBlock("span", _hoisted_53, " 停止 " + _toDisplayString(plan.value.operationCounts.qbStop) + " 个任务 · 退出 " + _toDisplayString(plan.value.operationCounts.qbRemoveKeepFiles) + " 个任务 · 解除 " + _toDisplayString(plan.value.operationCounts.unlinkFiles) + " 个文件入口 ", 1))
                            : (_openBlock(), _createElementBlock("span", _hoisted_54, [
                                _cache[41] || (_cache[41] = _createTextVNode(" 未生成可执行操作", -1)),
                                (plan.value.operationCounts.qbStop || plan.value.operationCounts.qbRemoveKeepFiles || plan.value.operationCounts.unlinkFiles || plan.value.operationCounts.moviepilotIndexes)
                                  ? (_openBlock(), _createElementBlock("span", _hoisted_55, "；下列关联影响仅供复核，不会执行"))
                                  : _createCommentVNode("", true)
                              ]))
                        ], 2),
                        (plan.value.blocks?.length)
                          ? (_openBlock(), _createElementBlock("ul", _hoisted_56, [
                              (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(plan.value.blocks, (issue, index) => {
                                return (_openBlock(), _createElementBlock("li", {
                                  key: _unref(issueKey)(issue, index)
                                }, _toDisplayString(issue.message), 1))
                              }), 128))
                            ]))
                          : _createCommentVNode("", true),
                        (planMissingFiles.value.length)
                          ? (_openBlock(), _createElementBlock("ul", _hoisted_57, [
                              (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(planMissingFiles.value, (missing) => {
                                return (_openBlock(), _createElementBlock("li", {
                                  key: missing.key
                                }, [
                                  _createTextVNode(" 缺失 " + _toDisplayString(missing.episode ? `${missing.episode} · ` : '') + _toDisplayString(missing.name) + "（来源：" + _toDisplayString(missing.source), 1),
                                  (missing.expectedSizeBytes)
                                    ? (_openBlock(), _createElementBlock(_Fragment, { key: 0 }, [
                                        _createTextVNode("，应有 " + _toDisplayString(_unref(formatBytes)(missing.expectedSizeBytes)), 1)
                                      ], 64))
                                    : _createCommentVNode("", true),
                                  _cache[42] || (_cache[42] = _createTextVNode("） ", -1))
                                ]))
                              }), 128))
                            ]))
                          : _createCommentVNode("", true),
                        (plan.value.warnings?.length)
                          ? (_openBlock(), _createElementBlock("ul", _hoisted_58, [
                              (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(plan.value.warnings, (issue, index) => {
                                return (_openBlock(), _createElementBlock("li", {
                                  key: _unref(issueKey)(issue, index)
                                }, _toDisplayString(issue.message), 1))
                              }), 128))
                            ]))
                          : _createCommentVNode("", true),
                        (plan.value.requiresSiteAcknowledgement)
                          ? (_openBlock(), _createElementBlock("label", _hoisted_59, [
                              _createElementVNode("input", {
                                checked: acknowledgeSiteRisk.value,
                                type: "checkbox",
                                onChange: _cache[4] || (_cache[4] = $event => (setSiteRisk($event.target.checked)))
                              }, null, 40, _hoisted_60),
                              _cache[43] || (_cache[43] = _createElementVNode("span", null, null, -1)),
                              _cache[44] || (_cache[44] = _createTextVNode(" 我已确认会影响私有站做种，并接受站点规则风险 ", -1))
                            ]))
                          : _createCommentVNode("", true),
                        (plan.value.requiresMissingFileAcknowledgement && planMode.value === 'delete')
                          ? (_openBlock(), _createElementBlock("label", _hoisted_61, [
                              _createElementVNode("input", {
                                checked: acknowledgeMissingFiles.value,
                                type: "checkbox",
                                onChange: _cache[5] || (_cache[5] = $event => (setMissingFileAcknowledgement($event.target.checked)))
                              }, null, 40, _hoisted_62),
                              _cache[45] || (_cache[45] = _createElementVNode("span", null, null, -1)),
                              _cache[46] || (_cache[46] = _createTextVNode(" 我已确认缺失的必需视频文件不会被删除，只清理其余已核验任务、文件和媒体索引 ", -1))
                            ]))
                          : _createCommentVNode("", true),
                        (planExpired.value)
                          ? (_openBlock(), _createElementBlock("div", _hoisted_63, [...(_cache[47] || (_cache[47] = [
                              _createElementVNode("strong", null, "安全预演已过期", -1),
                              _createElementVNode("span", null, "请关闭后重新生成。", -1)
                            ]))]))
                          : _createCommentVNode("", true)
                      ], 64))
                    : _createCommentVNode("", true),
              _createElementVNode("div", _hoisted_64, [
                _cache[49] || (_cache[49] = _createElementVNode("i", null, "盾", -1)),
                _createElementVNode("p", null, [
                  _createElementVNode("strong", null, _toDisplayString(executionEnabled.value ? '执行前还需第二次确认' : '执行引擎未启用'), 1),
                  _cache[48] || (_cache[48] = _createElementVNode("span", null, "最终执行前复核当前清单，执行器只回读所选资源的 qB、路径和硬链接。", -1))
                ])
              ]),
              (executeError.value && !executeResult.value)
                ? (_openBlock(), _createElementBlock("div", _hoisted_65, [
                    _cache[50] || (_cache[50] = _createElementVNode("strong", null, "执行未开始", -1)),
                    _createElementVNode("span", null, _toDisplayString(executeError.value), 1)
                  ]))
                : _createCommentVNode("", true),
              (executeResult.value)
                ? (_openBlock(), _createElementBlock("div", _hoisted_66, [
                    _createElementVNode("strong", null, _toDisplayString(currentAction.value?.title) + "已完成", 1),
                    _createElementVNode("span", null, [
                      _createTextVNode(" 停止 " + _toDisplayString(executeResult.value.qbStopped) + " · 退出 " + _toDisplayString(executeResult.value.qbRemoved) + " · 删除文件入口 " + _toDisplayString(executeResult.value.filesDeleted) + " · 清理索引 " + _toDisplayString(executeResult.value.moviepilotIndexesDeleted) + " ", 1),
                      (executeResult.value.missingFilesAlreadyAbsent)
                        ? (_openBlock(), _createElementBlock(_Fragment, { key: 0 }, [
                            _createTextVNode(" · 已核对缺失入口 " + _toDisplayString(executeResult.value.missingFilesAlreadyAbsent) + " 个（不计释放量） ", 1)
                          ], 64))
                        : _createCommentVNode("", true)
                    ]),
                    (executeResult.value.snapshotRefreshPending)
                      ? (_openBlock(), _createElementBlock("span", _hoisted_67, _toDisplayString(planMode.value === 'delete' ? '已从当前列表移除，请刷新资源清单后继续操作。' : '操作已完成，请刷新资源清单后继续操作。'), 1))
                      : _createCommentVNode("", true),
                    _createElementVNode("button", {
                      type: "button",
                      onClick: closePlan
                    }, "完成")
                  ]))
                : (finalConfirmation.value)
                  ? (_openBlock(), _createElementBlock("div", _hoisted_68, [
                      _cache[51] || (_cache[51] = _createElementVNode("strong", null, "再次确认：系统将立即执行这份计划", -1)),
                      _createElementVNode("div", null, [
                        _createElementVNode("button", {
                          type: "button",
                          disabled: executing.value,
                          onClick: _cache[6] || (_cache[6] = $event => (finalConfirmation.value = false))
                        }, "返回", 8, _hoisted_69),
                        _createElementVNode("button", {
                          class: _normalizeClass({ danger: planMode.value === 'delete' }),
                          type: "button",
                          disabled: executing.value || !inventoryCurrent.value || planExpired.value,
                          onClick: executePlan
                        }, _toDisplayString(executing.value ? '正在定向复核…' : `确认${currentAction.value?.title}`), 11, _hoisted_70)
                      ])
                    ]))
                  : (_openBlock(), _createElementBlock("button", {
                      key: 6,
                      class: "confirm-button",
                      type: "button",
                      disabled: !executionEnabled.value || !inventoryCurrent.value || !plan.value?.canExecute || planExpired.value,
                      onClick: _cache[7] || (_cache[7] = $event => (finalConfirmation.value = true))
                    }, " 进入最终确认 ", 8, _hoisted_71))
            ])
          ]))
        : _createCommentVNode("", true),
      (gapOpen.value)
        ? (_openBlock(), _createElementBlock("div", {
            key: 2,
            class: "modal-backdrop",
            onClick: _cache[9] || (_cache[9] = _withModifiers($event => (gapOpen.value = false), ["self"]))
          }, [
            _createElementVNode("section", _hoisted_72, [
              _createElementVNode("header", null, [
                _cache[52] || (_cache[52] = _createElementVNode("div", null, [
                  _createElementVNode("span", null, "H&R 实时保护"),
                  _createElementVNode("h2", null, "H&R 缺口明细")
                ], -1)),
                _createElementVNode("button", {
                  onClick: _cache[8] || (_cache[8] = $event => (gapOpen.value = false))
                }, "×")
              ]),
              (gapLoading.value)
                ? (_openBlock(), _createElementBlock("div", _hoisted_73, "正在核对…"))
                : _createCommentVNode("", true),
              (gapError.value)
                ? (_openBlock(), _createElementBlock("div", _hoisted_74, _toDisplayString(gapError.value), 1))
                : _createCommentVNode("", true),
              (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(gaps.value, (item) => {
                return (_openBlock(), _createElementBlock("div", {
                  key: item.title,
                  class: "gap-row"
                }, [
                  _createElementVNode("p", null, [
                    _createElementVNode("strong", null, _toDisplayString(item.title), 1),
                    _createElementVNode("span", null, _toDisplayString(item.linkedResourceTitle || '尚未精确关联媒体'), 1)
                  ]),
                  _createElementVNode("b", null, _toDisplayString(item.qbTaskPresent ? 'qB 已存在' : item.coveredByCandidate ? '候选恢复中' : '任务缺失'), 1)
                ]))
              }), 128))
            ])
          ]))
        : _createCommentVNode("", true),
      (recoveryOpen.value)
        ? (_openBlock(), _createElementBlock("div", {
            key: 3,
            class: "modal-backdrop",
            onClick: _cache[12] || (_cache[12] = _withModifiers($event => (recoveryOpen.value = false), ["self"]))
          }, [
            _createElementVNode("section", _hoisted_75, [
              _createElementVNode("header", null, [
                _cache[53] || (_cache[53] = _createElementVNode("div", null, [
                  _createElementVNode("span", null, "失败关闭"),
                  _createElementVNode("h2", null, "恢复未完成清理")
                ], -1)),
                _createElementVNode("button", {
                  onClick: _cache[10] || (_cache[10] = $event => (recoveryOpen.value = false))
                }, "×")
              ]),
              (recoveryLoading.value)
                ? (_openBlock(), _createElementBlock("div", _hoisted_76, "正在读取事务…"))
                : _createCommentVNode("", true),
              (recoveryError.value)
                ? (_openBlock(), _createElementBlock("div", _hoisted_77, _toDisplayString(recoveryError.value), 1))
                : _createCommentVNode("", true),
              (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(recoveries.value, (item) => {
                return (_openBlock(), _createElementBlock("div", {
                  key: item.planId,
                  class: "recovery-row"
                }, [
                  _createElementVNode("p", null, [
                    _createElementVNode("strong", null, _toDisplayString(item.mode) + " · " + _toDisplayString(item.phase), 1),
                    _createElementVNode("span", null, _toDisplayString(item.planId.slice(-10)), 1)
                  ]),
                  _createElementVNode("button", {
                    type: "button",
                    onClick: $event => (chooseRecovery(item, 'rollback'))
                  }, "回滚", 8, _hoisted_78),
                  _createElementVNode("button", {
                    type: "button",
                    onClick: $event => (chooseRecovery(item, 'finalize'))
                  }, "完成原事务", 8, _hoisted_79)
                ]))
              }), 128)),
              (recoveryTarget.value)
                ? (_openBlock(), _createElementBlock("div", _hoisted_80, [
                    _createElementVNode("label", null, [
                      _cache[54] || (_cache[54] = _createTextVNode("输入确认短语 ", -1)),
                      _createElementVNode("code", null, _toDisplayString(recoveryExpectedPhrase()), 1)
                    ]),
                    _withDirectives(_createElementVNode("input", {
                      "onUpdate:modelValue": _cache[11] || (_cache[11] = $event => ((recoveryPhrase).value = $event)),
                      autocomplete: "off"
                    }, null, 512), [
                      [_vModelText, recoveryPhrase.value]
                    ]),
                    _createElementVNode("button", {
                      type: "button",
                      disabled: recovering.value || recoveryPhrase.value !== recoveryExpectedPhrase(),
                      onClick: runRecovery
                    }, _toDisplayString(recovering.value ? '处理中…' : '执行恢复'), 9, _hoisted_81)
                  ]))
                : _createCommentVNode("", true)
            ])
          ]))
        : _createCommentVNode("", true),
      (settingsOpen.value)
        ? (_openBlock(), _createElementBlock("div", {
            key: 4,
            class: "modal-backdrop",
            onClick: _withModifiers(closeSettings, ["self"])
          }, [
            _createElementVNode("section", _hoisted_82, [
              _createElementVNode("header", null, [
                _cache[55] || (_cache[55] = _createElementVNode("div", null, [
                  _createElementVNode("span", null, "清理台配置"),
                  _createElementVNode("h2", { id: "storage-cleanup-settings-title" }, "设置")
                ], -1)),
                _createElementVNode("button", {
                  type: "button",
                  "aria-label": "关闭设置",
                  onClick: closeSettings
                }, "×")
              ]),
              _createVNode(Config, {
                api: props.api,
                "plugin-id": props.pluginId,
                onConfigSaved: handleConfigSaved
              }, null, 8, ["api", "plugin-id"])
            ])
          ]))
        : _createCommentVNode("", true)
    ]))
  ]))
}
}

};
const AppPage = /*#__PURE__*/_export_sfc(_sfc_main, [['__scopeId',"data-v-87abf0ae"]]);

export { createLatestPlanApi as c, AppPage as default };
