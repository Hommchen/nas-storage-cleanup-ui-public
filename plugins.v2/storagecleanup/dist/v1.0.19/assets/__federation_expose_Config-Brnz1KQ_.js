import { importShared } from './__federation_fn_import-JrT3xvdd.js';

const _export_sfc = (sfc, props) => {
  const target = sfc.__vccOpts || sfc;
  for (const [key, val] of props) {
    target[key] = val;
  }
  return target;
};

const {createElementVNode:_createElementVNode,openBlock:_openBlock,createElementBlock:_createElementBlock,createCommentVNode:_createCommentVNode,toDisplayString:_toDisplayString,renderList:_renderList,Fragment:_Fragment,normalizeClass:_normalizeClass,vModelCheckbox:_vModelCheckbox,withDirectives:_withDirectives,vModelSelect:_vModelSelect,createTextVNode:_createTextVNode,vModelText:_vModelText} = await importShared('vue');


const _hoisted_1 = { class: "config-page" };
const _hoisted_2 = {
  key: 0,
  class: "notice"
};
const _hoisted_3 = {
  key: 1,
  class: "discovery-card"
};
const _hoisted_4 = { class: "discovery-heading" };
const _hoisted_5 = { class: "discovery-actions" };
const _hoisted_6 = ["disabled"];
const _hoisted_7 = ["disabled"];
const _hoisted_8 = {
  key: 0,
  class: "discovery-results"
};
const _hoisted_9 = { key: 0 };
const _hoisted_10 = ["disabled"];
const _hoisted_11 = {
  key: 1,
  class: "discovery-error"
};
const _hoisted_12 = ["disabled"];
const _hoisted_13 = {
  key: 2,
  class: "hr-settings"
};
const _hoisted_14 = { class: "hr-heading" };
const _hoisted_15 = { class: "switch-label" };
const _hoisted_16 = { class: "hr-rows" };
const _hoisted_17 = {
  key: 0,
  class: "notice"
};
const _hoisted_18 = ["onUpdate:modelValue"];
const _hoisted_19 = ["value"];
const _hoisted_20 = ["onUpdate:modelValue"];
const _hoisted_21 = ["disabled", "onClick"];
const _hoisted_22 = { class: "hr-actions" };
const _hoisted_23 = ["disabled"];
const _hoisted_24 = { key: 0 };
const _hoisted_25 = ["open"];
const _hoisted_26 = { class: "form-grid" };
const _hoisted_27 = { class: "wide" };
const _hoisted_28 = { class: "wide" };
const _hoisted_29 = ["disabled"];
const _hoisted_30 = {
  key: 4,
  class: "error"
};
const _hoisted_31 = {
  key: 5,
  class: "success"
};
const _hoisted_32 = {
  key: 6,
  class: "probe"
};
const _hoisted_33 = { key: 0 };
const _hoisted_34 = { key: 1 };
const _hoisted_35 = {
  key: 2,
  class: "probe-details"
};

const {computed,onMounted,reactive,ref} = await importShared('vue');



const _sfc_main = {
  __name: 'Config',
  props: {
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'StorageCleanup' },
},
  emits: ['config-saved'],
  setup(__props, { emit: __emit }) {

const props = __props;
const emit = __emit;

const form = reactive({
  version: 1,
  qb_url: '',
  media_index_db: '',
  moviepilot_db: '',
  qb_backup: '',
  execution_backup: '',
  hit_and_run_enabled: false,
  hit_and_run_sites: [],
  allowed_roots_text: '',
  quarantine_roots_text: '',
});
const loading = ref(true);
const saving = ref(false);
const error = ref('');
const message = ref('');
const probe = ref(null);
const discovery = ref(null);
const discovering = ref(false);
const discoveryError = ref('');
const advancedOpen = ref(false);
const moviepilotSites = ref([]);

const pluginBase = computed(() => `plugin/${props.pluginId || 'StorageCleanup'}`);

function unwrap(response) {
  if (response && Object.prototype.hasOwnProperty.call(response, 'data')) {
    return response.data
  }
  return response
}

function applyConfig(config) {
  const hitAndRunSites = Array.isArray(config.hit_and_run_sites)
    ? config.hit_and_run_sites.map(item => ({
      site: String(item?.site || '').trim(),
      path: String(item?.path || '').trim(),
      parser: String(item?.parser || 'nexusphp_myhr').trim(),
    }))
    : [];
  Object.assign(form, {
    ...config,
    hit_and_run_enabled: config.hit_and_run_enabled === true,
    hit_and_run_sites: hitAndRunSites,
    media_index_db: config.media_index_db || config.jellyfin_db || '',
    allowed_roots_text: (config.allowed_roots || []).join('\n'),
    quarantine_roots_text: Object.entries(config.quarantine_roots || {})
      .map(([volume, target]) => `${volume}=${target}`)
      .join('\n'),
  });
}

function siteOptions(currentSite) {
  const options = [...moviepilotSites.value];
  const current = String(currentSite || '').trim();
  if (current && !options.some(item => item.site === current)) {
    options.push({ site: current, label: `${current}（当前配置）` });
  }
  return options
}

function addHitAndRunSite() {
  const first = moviepilotSites.value[0]?.site || 'btschool.club';
  form.hit_and_run_sites.push({
    site: first,
    path: first === 'btschool.club' ? '/myhr.php' : '',
    parser: 'nexusphp_myhr',
  });
}

function removeHitAndRunSite(index) {
  form.hit_and_run_sites.splice(index, 1);
}

function parseLines(value) {
  return String(value || '')
    .split('\n')
    .map(item => item.trim())
    .filter(Boolean)
}

function buildConfig() {
  const quarantine_roots = {};
  for (const line of parseLines(form.quarantine_roots_text)) {
    const separator = line.indexOf('=');
    if (separator <= 0 || separator === line.length - 1) {
      throw new Error('隔离目录格式应为：卷根目录=隔离目录。')
    }
    quarantine_roots[line.slice(0, separator).trim()] = line.slice(separator + 1).trim();
  }
  return {
    version: 1,
    qb_url: form.qb_url,
    media_index_db: form.media_index_db,
    moviepilot_db: form.moviepilot_db,
    qb_backup: form.qb_backup,
    execution_backup: form.execution_backup,
    hit_and_run_enabled: form.hit_and_run_enabled === true,
    hit_and_run_sites: form.hit_and_run_sites
      .map(item => ({
        site: String(item.site || '').trim(),
        path: String(item.path || '').trim(),
        parser: String(item.parser || 'nexusphp_myhr').trim(),
      }))
      .filter(item => item.site || item.path),
    allowed_roots: parseLines(form.allowed_roots_text),
    quarantine_roots,
  }
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    if (!props.api.get) throw new Error('MoviePilot 没有提供插件 API。')
    const payload = unwrap(await props.api.get(`${pluginBase.value}/config`));
    if (!payload?.ok || !payload.config) throw new Error(payload?.error?.message || '无法读取清理台配置。')
    applyConfig(payload.config);
    moviepilotSites.value = payload.hitAndRunSites || [];
    probe.value = payload.probe || null;
    if (!probe.value?.ok) void discover();
  } catch (err) {
    error.value = err?.message || '无法读取清理台配置。';
  } finally {
    loading.value = false;
  }
}

async function discover() {
  if (discovering.value) return
  discovering.value = true;
  discoveryError.value = '';
  try {
    if (!props.api.get) throw new Error('MoviePilot 没有提供插件 API。')
    const payload = unwrap(await props.api.get(`${pluginBase.value}/discover`));
    if (!payload?.ok || !payload.config) {
      throw new Error(payload?.error?.message || '自动发现失败。')
    }
    discovery.value = payload;
    if ((payload.checks || []).some(item => item.ambiguous || (!item.found && !item.optional && !item.willCreate))) {
      advancedOpen.value = true;
    }
  } catch (err) {
    discoveryError.value = err?.message || '自动发现失败。';
    advancedOpen.value = true;
  } finally {
    discovering.value = false;
  }
}

async function applyDiscovery() {
  if (!discovery.value?.config || !discovery.value?.ready || saving.value) return
  saving.value = true;
  error.value = '';
  message.value = '';
  try {
    const payload = unwrap(await props.api.post(`${pluginBase.value}/config`, {
      config: discovery.value.config,
    }));
    if (!payload?.ok || !payload.config) {
      throw new Error(payload?.error?.message || '自动配置保存失败。')
    }
    applyConfig(payload.config);
    moviepilotSites.value = payload.hitAndRunSites || moviepilotSites.value;
    emit('config-saved', payload.config);
    probe.value = payload.probe || null;
    message.value = probe.value?.ok
      ? '自动识别完成，路径探测通过；请刷新资源清单。'
      : '已应用自动识别结果，但仍有项目未就绪；清理操作保持锁定。';
  } catch (err) {
    const payload = err?.response?.data || err?.data;
    probe.value = payload?.probe || probe.value;
    error.value = err?.message || payload?.error?.message || '自动配置保存失败。';
  } finally {
    saving.value = false;
  }
}

async function save() {
  saving.value = true;
  error.value = '';
  message.value = '';
  try {
    const config = buildConfig();
    const payload = unwrap(await props.api.post(`${pluginBase.value}/config`, { config }));
    if (!payload?.ok || !payload.config) throw new Error(payload?.error?.message || '配置保存失败。')
    applyConfig(payload.config);
    moviepilotSites.value = payload.hitAndRunSites || moviepilotSites.value;
    emit('config-saved', payload.config);
    probe.value = payload.probe || null;
    message.value = probe.value?.ok
      ? '配置已保存，路径探测通过；请刷新资源清单。'
      : '配置已保存，但仍有路径未就绪；清理操作保持锁定。';
  } catch (err) {
    const payload = err?.response?.data || err?.data;
    probe.value = payload?.probe || probe.value;
    error.value = err?.message || payload?.error?.message || '配置保存失败。';
  } finally {
    saving.value = false;
  }
}

onMounted(load);

return (_ctx, _cache) => {
  return (_openBlock(), _createElementBlock("section", _hoisted_1, [
    _cache[26] || (_cache[26] = _createElementVNode("header", null, [
      _createElementVNode("h2", null, "存储清理设置"),
      _createElementVNode("p", null, "一般无需填写，先点“自动识别”；识别失败再用手动配置。")
    ], -1)),
    (loading.value)
      ? (_openBlock(), _createElementBlock("div", _hoisted_2, "正在读取配置…"))
      : (_openBlock(), _createElementBlock("section", _hoisted_3, [
          _createElementVNode("div", _hoisted_4, [
            _cache[11] || (_cache[11] = _createElementVNode("div", null, [
              _createElementVNode("strong", null, "自动识别"),
              _createElementVNode("span", null, "读取 MoviePilot、qB 和媒体目录；媒体库索引可留空。候选不唯一时不会自动猜。")
            ], -1)),
            _createElementVNode("div", _hoisted_5, [
              _createElementVNode("button", {
                type: "button",
                disabled: discovering.value || saving.value,
                onClick: discover
              }, _toDisplayString(discovering.value ? '识别中…' : '自动识别'), 9, _hoisted_6),
              (!advancedOpen.value)
                ? (_openBlock(), _createElementBlock("button", {
                    key: 0,
                    class: "secondary",
                    type: "button",
                    disabled: saving.value,
                    onClick: _cache[0] || (_cache[0] = $event => (advancedOpen.value = true))
                  }, " 手动配置 ", 8, _hoisted_7))
                : _createCommentVNode("", true)
            ])
          ]),
          (discovery.value)
            ? (_openBlock(), _createElementBlock("div", _hoisted_8, [
                (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(discovery.value.checks || [], (item) => {
                  return (_openBlock(), _createElementBlock("div", {
                    key: item.key,
                    class: "discovery-row"
                  }, [
                    _createElementVNode("div", null, [
                      _createElementVNode("span", null, _toDisplayString(item.label), 1),
                      (item.ambiguous)
                        ? (_openBlock(), _createElementBlock("small", _hoisted_9, "候选：" + _toDisplayString((item.candidates || []).join('；')), 1))
                        : _createCommentVNode("", true)
                    ]),
                    _createElementVNode("b", {
                      class: _normalizeClass(item.ambiguous ? 'missing' : item.found || item.willCreate ? 'found' : item.optional ? 'optional' : 'missing')
                    }, _toDisplayString(item.ambiguous ? '发现多个候选，需手动选择' : item.found ? '已找到' : item.willCreate ? '将自动创建' : item.optional ? '未配置（可选）' : '需管理员处理'), 3)
                  ]))
                }), 128)),
                _createElementVNode("button", {
                  class: "apply-discovery",
                  type: "button",
                  disabled: saving.value || !discovery.value.ready,
                  onClick: applyDiscovery
                }, _toDisplayString(saving.value ? '应用中…' : '应用识别结果并验证'), 9, _hoisted_10)
              ]))
            : _createCommentVNode("", true),
          (discoveryError.value)
            ? (_openBlock(), _createElementBlock("div", _hoisted_11, [
                _createElementVNode("span", null, _toDisplayString(discoveryError.value) + " 请改用手动配置。", 1),
                _createElementVNode("button", {
                  type: "button",
                  disabled: saving.value,
                  onClick: _cache[1] || (_cache[1] = $event => (advancedOpen.value = true))
                }, "打开手动配置", 8, _hoisted_12)
              ]))
            : _createCommentVNode("", true)
        ])),
    (!loading.value)
      ? (_openBlock(), _createElementBlock("section", _hoisted_13, [
          _createElementVNode("div", _hoisted_14, [
            _cache[12] || (_cache[12] = _createElementVNode("div", null, [
              _createElementVNode("strong", null, "Hit and Run（H&R）"),
              _createElementVNode("p", null, "默认关闭。开启后，后台会随资源快照刷新，按每个站点配置的入口路径查询；Cookie 和 UA 继续使用 MoviePilot 登录态。")
            ], -1)),
            _createElementVNode("label", _hoisted_15, [
              _withDirectives(_createElementVNode("input", {
                "onUpdate:modelValue": _cache[2] || (_cache[2] = $event => ((form.hit_and_run_enabled) = $event)),
                type: "checkbox"
              }, null, 512), [
                [_vModelCheckbox, form.hit_and_run_enabled]
              ]),
              _createElementVNode("span", null, _toDisplayString(form.hit_and_run_enabled ? '已开启' : '默认关闭'), 1)
            ])
          ]),
          _createElementVNode("div", _hoisted_16, [
            (!form.hit_and_run_sites.length)
              ? (_openBlock(), _createElementBlock("div", _hoisted_17, "尚未配置 H&R 站点；即使总开关打开，H&R 也不会生效。"))
              : _createCommentVNode("", true),
            (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(form.hit_and_run_sites, (item, index) => {
              return (_openBlock(), _createElementBlock("div", {
                key: `${item.site}-${index}`,
                class: "hr-row"
              }, [
                _createElementVNode("label", null, [
                  _cache[13] || (_cache[13] = _createTextVNode(" 站点 ", -1)),
                  _withDirectives(_createElementVNode("select", {
                    "onUpdate:modelValue": $event => ((item.site) = $event)
                  }, [
                    (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(siteOptions(item.site), (site) => {
                      return (_openBlock(), _createElementBlock("option", {
                        key: site.site,
                        value: site.site
                      }, _toDisplayString(site.label || site.site), 9, _hoisted_19))
                    }), 128))
                  ], 8, _hoisted_18), [
                    [_vModelSelect, item.site]
                  ])
                ]),
                _createElementVNode("label", null, [
                  _cache[14] || (_cache[14] = _createTextVNode(" H&R 列表入口路径 ", -1)),
                  _withDirectives(_createElementVNode("input", {
                    "onUpdate:modelValue": $event => ((item.path) = $event),
                    autocomplete: "off",
                    placeholder: "例如：/myhr.php"
                  }, null, 8, _hoisted_20), [
                    [_vModelText, item.path]
                  ])
                ]),
                _cache[15] || (_cache[15] = _createElementVNode("span", { class: "parser-hint" }, "解析器：NexusPHP 标准", -1)),
                _createElementVNode("button", {
                  class: "remove-hr",
                  type: "button",
                  disabled: saving.value,
                  onClick: $event => (removeHitAndRunSite(index))
                }, "删除", 8, _hoisted_21)
              ]))
            }), 128))
          ]),
          _createElementVNode("div", _hoisted_22, [
            _createElementVNode("button", {
              type: "button",
              disabled: saving.value,
              onClick: addHitAndRunSite
            }, "添加站点", 8, _hoisted_23),
            (!moviepilotSites.value.length)
              ? (_openBlock(), _createElementBlock("span", _hoisted_24, "当前未读取到 MoviePilot 已启用站点；可保留已有配置，后台会在站点可用后自动测试。"))
              : _createCommentVNode("", true)
          ])
        ]))
      : _createCommentVNode("", true),
    (!loading.value)
      ? (_openBlock(), _createElementBlock("details", {
          key: 3,
          class: "advanced-settings",
          open: advancedOpen.value,
          onToggle: _cache[10] || (_cache[10] = $event => (advancedOpen.value = $event.target.open))
        }, [
          _cache[23] || (_cache[23] = _createElementVNode("summary", null, "手动配置（自动识别失败时使用）", -1)),
          _cache[24] || (_cache[24] = _createElementVNode("p", null, "从 NAS 文件管理器复制路径；必须是清理台服务能访问到的路径。媒体库索引可以留空。", -1)),
          _createElementVNode("div", _hoisted_26, [
            _createElementVNode("label", null, [
              _cache[16] || (_cache[16] = _createTextVNode("qBittorrent 地址", -1)),
              _withDirectives(_createElementVNode("input", {
                "onUpdate:modelValue": _cache[3] || (_cache[3] = $event => ((form.qb_url) = $event)),
                autocomplete: "off",
                placeholder: "例：http://127.0.0.1:8080"
              }, null, 512), [
                [_vModelText, form.qb_url]
              ])
            ]),
            _createElementVNode("label", null, [
              _cache[17] || (_cache[17] = _createTextVNode("MoviePilot 数据库", -1)),
              _withDirectives(_createElementVNode("input", {
                "onUpdate:modelValue": _cache[4] || (_cache[4] = $event => ((form.moviepilot_db) = $event)),
                autocomplete: "off",
                placeholder: "MoviePilot 容器内 user.db 路径"
              }, null, 512), [
                [_vModelText, form.moviepilot_db]
              ])
            ]),
            _createElementVNode("label", null, [
              _cache[18] || (_cache[18] = _createTextVNode("媒体库索引（可选）", -1)),
              _withDirectives(_createElementVNode("input", {
                "onUpdate:modelValue": _cache[5] || (_cache[5] = $event => ((form.media_index_db) = $event)),
                autocomplete: "off",
                placeholder: "Jellyfin / Emby 数据库路径，可留空"
              }, null, 512), [
                [_vModelText, form.media_index_db]
              ])
            ]),
            _createElementVNode("label", null, [
              _cache[19] || (_cache[19] = _createTextVNode("qB 种子备份目录", -1)),
              _withDirectives(_createElementVNode("input", {
                "onUpdate:modelValue": _cache[6] || (_cache[6] = $event => ((form.qb_backup) = $event)),
                autocomplete: "off",
                placeholder: "qB 备份目录"
              }, null, 512), [
                [_vModelText, form.qb_backup]
              ])
            ]),
            _createElementVNode("label", null, [
              _cache[20] || (_cache[20] = _createTextVNode("清理事务备份目录", -1)),
              _withDirectives(_createElementVNode("input", {
                "onUpdate:modelValue": _cache[7] || (_cache[7] = $event => ((form.execution_backup) = $event)),
                autocomplete: "off",
                placeholder: "清理台可写的备份目录"
              }, null, 512), [
                [_vModelText, form.execution_backup]
              ])
            ]),
            _createElementVNode("label", _hoisted_27, [
              _cache[21] || (_cache[21] = _createTextVNode("允许扫描/清理的根目录（每行一个）", -1)),
              _withDirectives(_createElementVNode("textarea", {
                "onUpdate:modelValue": _cache[8] || (_cache[8] = $event => ((form.allowed_roots_text) = $event)),
                rows: "5",
                placeholder: "下载完成目录、电影目录、电视剧目录"
              }, null, 512), [
                [_vModelText, form.allowed_roots_text]
              ])
            ]),
            _createElementVNode("label", _hoisted_28, [
              _cache[22] || (_cache[22] = _createTextVNode("隔离目录映射（每行：卷根目录=隔离目录）", -1)),
              _withDirectives(_createElementVNode("textarea", {
                "onUpdate:modelValue": _cache[9] || (_cache[9] = $event => ((form.quarantine_roots_text) = $event)),
                rows: "3",
                placeholder: "例如：/mnt/data=/mnt/data/.storage-cleanup-quarantine"
              }, null, 512), [
                [_vModelText, form.quarantine_roots_text]
              ])
            ])
          ]),
          _createElementVNode("button", {
            class: "save",
            disabled: loading.value || saving.value,
            onClick: save
          }, _toDisplayString(saving.value ? '保存中…' : '保存手动配置并探测'), 9, _hoisted_29)
        ], 40, _hoisted_25))
      : _createCommentVNode("", true),
    (error.value)
      ? (_openBlock(), _createElementBlock("div", _hoisted_30, _toDisplayString(error.value), 1))
      : _createCommentVNode("", true),
    (message.value)
      ? (_openBlock(), _createElementBlock("div", _hoisted_31, _toDisplayString(message.value), 1))
      : _createCommentVNode("", true),
    (probe.value)
      ? (_openBlock(), _createElementBlock("div", _hoisted_32, [
          _createElementVNode("strong", null, _toDisplayString(probe.value.ok ? '只读探测通过' : '只读探测未通过'), 1),
          (probe.value.missing?.length)
            ? (_openBlock(), _createElementBlock("span", _hoisted_33, "还有 " + _toDisplayString(probe.value.missing.length) + " 项路径未找到，请展开管理员配置查看。", 1))
            : _createCommentVNode("", true),
          (probe.value.problems?.length)
            ? (_openBlock(), _createElementBlock("span", _hoisted_34, "有 " + _toDisplayString(probe.value.problems.length) + " 项安全校验未通过，请展开管理员配置查看。", 1))
            : _createCommentVNode("", true),
          (probe.value.missing?.length || probe.value.problems?.length)
            ? (_openBlock(), _createElementBlock("details", _hoisted_35, [
                _cache[25] || (_cache[25] = _createElementVNode("summary", null, "查看管理员诊断", -1)),
                (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(probe.value.missing || [], (item) => {
                  return (_openBlock(), _createElementBlock("span", {
                    key: `missing-${item}`
                  }, "未找到：" + _toDisplayString(item), 1))
                }), 128)),
                (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(probe.value.problems || [], (item) => {
                  return (_openBlock(), _createElementBlock("span", { key: item }, _toDisplayString(item), 1))
                }), 128))
              ]))
            : _createCommentVNode("", true)
        ]))
      : _createCommentVNode("", true)
  ]))
}
}

};
const Config = /*#__PURE__*/_export_sfc(_sfc_main, [['__scopeId',"data-v-cd23536d"]]);

export { _export_sfc as _, Config as default };
