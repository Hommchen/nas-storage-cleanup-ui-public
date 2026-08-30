<script setup>
import { computed, onMounted, reactive, ref } from 'vue'

const props = defineProps({
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: 'StorageCleanup' },
})
const emit = defineEmits(['config-saved'])

const form = reactive({
  version: 1,
  qb_url: '',
  media_index_db: '',
  moviepilot_db: '',
  qb_backup: '',
  execution_backup: '',
  snapshot_max_age_seconds: 3600,
  hit_and_run_enabled: false,
  hit_and_run_sites: [],
  allowed_roots_text: '',
  quarantine_roots_text: '',
})
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const message = ref('')
const probe = ref(null)
const discovery = ref(null)
const discovering = ref(false)
const discoveryError = ref('')
const advancedOpen = ref(false)
const moviepilotSites = ref([])

const pluginBase = computed(() => `plugin/${props.pluginId || 'StorageCleanup'}`)

function unwrap(response) {
  if (response && Object.prototype.hasOwnProperty.call(response, 'data')) {
    return response.data
  }
  return response
}

function apiErrorMessage(err, fallback) {
  const payload = err?.response?.data || err?.data || {}
  return payload?.error?.message || payload?.message || err?.message || fallback
}

function applyConfig(config) {
  const hitAndRunSites = Array.isArray(config.hit_and_run_sites)
    ? config.hit_and_run_sites.map(item => ({
      site: String(item?.site || '').trim(),
      path: String(item?.path || '').trim(),
      parser: String(item?.parser || 'nexusphp_myhr').trim(),
    }))
    : []
  Object.assign(form, {
    ...config,
    hit_and_run_enabled: config.hit_and_run_enabled === true,
    hit_and_run_sites: hitAndRunSites,
    media_index_db: config.media_index_db || config.jellyfin_db || '',
    allowed_roots_text: (config.allowed_roots || []).join('\n'),
    quarantine_roots_text: Object.entries(config.quarantine_roots || {})
      .map(([volume, target]) => `${volume}=${target}`)
      .join('\n'),
  })
}

function siteOptions(currentSite) {
  const options = [...moviepilotSites.value]
  const current = String(currentSite || '').trim()
  if (current && !options.some(item => item.site === current)) {
    options.push({ site: current, label: `${current}（当前配置）` })
  }
  return options
}

function addHitAndRunSite() {
  const first = moviepilotSites.value[0]?.site || 'btschool.club'
  form.hit_and_run_sites.push({
    site: first,
    path: first === 'btschool.club' ? '/myhr.php' : '',
    parser: 'nexusphp_myhr',
  })
}

function removeHitAndRunSite(index) {
  form.hit_and_run_sites.splice(index, 1)
}

function parseLines(value) {
  return String(value || '')
    .split('\n')
    .map(item => item.trim())
    .filter(Boolean)
}

function buildConfig() {
  const quarantine_roots = {}
  for (const line of parseLines(form.quarantine_roots_text)) {
    const separator = line.indexOf('=')
    if (separator <= 0 || separator === line.length - 1) {
      throw new Error('隔离目录格式应为：卷根目录=隔离目录。')
    }
    quarantine_roots[line.slice(0, separator).trim()] = line.slice(separator + 1).trim()
  }
  return {
    version: 1,
    qb_url: form.qb_url,
    media_index_db: form.media_index_db,
    moviepilot_db: form.moviepilot_db,
    qb_backup: form.qb_backup,
    execution_backup: form.execution_backup,
    snapshot_max_age_seconds: Number(form.snapshot_max_age_seconds),
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
  loading.value = true
  error.value = ''
  try {
    if (!props.api.get) throw new Error('MoviePilot 没有提供插件 API。')
    const payload = unwrap(await props.api.get(`${pluginBase.value}/config`))
    if (!payload?.ok || !payload.config) throw new Error(payload?.error?.message || '无法读取清理台配置。')
    applyConfig(payload.config)
    moviepilotSites.value = payload.hitAndRunSites || []
    probe.value = payload.probe || null
    if (!probe.value?.ok) void discover()
  } catch (err) {
    error.value = apiErrorMessage(err, '无法读取清理台配置。')
  } finally {
    loading.value = false
  }
}

async function discover() {
  if (discovering.value) return
  discovering.value = true
  discoveryError.value = ''
  try {
    if (!props.api.get) throw new Error('MoviePilot 没有提供插件 API。')
    const payload = unwrap(await props.api.get(`${pluginBase.value}/discover`))
    if (!payload?.ok || !payload.config) {
      throw new Error(payload?.error?.message || '自动发现失败。')
    }
    discovery.value = payload
    if ((payload.checks || []).some(item => item.ambiguous || (!item.found && !item.optional && !item.willCreate))) {
      advancedOpen.value = true
    }
  } catch (err) {
    discoveryError.value = apiErrorMessage(err, '自动发现失败。')
    advancedOpen.value = true
  } finally {
    discovering.value = false
  }
}

async function applyDiscovery() {
  if (!discovery.value?.config || !discovery.value?.ready || saving.value) return
  saving.value = true
  error.value = ''
  message.value = ''
  try {
    const payload = unwrap(await props.api.post(`${pluginBase.value}/config`, {
      config: discovery.value.config,
    }))
    if (!payload?.ok || !payload.config) {
      throw new Error(payload?.error?.message || '自动配置保存失败。')
    }
    applyConfig(payload.config)
    moviepilotSites.value = payload.hitAndRunSites || moviepilotSites.value
    emit('config-saved', payload.config)
    probe.value = payload.probe || null
    message.value = probe.value?.ok
      ? '自动识别完成，路径探测通过；请刷新资源清单。'
      : '已应用自动识别结果，但仍有项目未就绪；清理操作保持锁定。'
  } catch (err) {
    const payload = err?.response?.data || err?.data
    probe.value = payload?.probe || probe.value
    error.value = apiErrorMessage(err, '自动配置保存失败。')
  } finally {
    saving.value = false
  }
}

async function save() {
  saving.value = true
  error.value = ''
  message.value = ''
  try {
    const config = buildConfig()
    const payload = unwrap(await props.api.post(`${pluginBase.value}/config`, { config }))
    if (!payload?.ok || !payload.config) throw new Error(payload?.error?.message || '配置保存失败。')
    applyConfig(payload.config)
    moviepilotSites.value = payload.hitAndRunSites || moviepilotSites.value
    emit('config-saved', payload.config)
    probe.value = payload.probe || null
    message.value = probe.value?.ok
      ? '配置已保存，路径探测通过；请刷新资源清单。'
      : '配置已保存，但仍有路径未就绪；清理操作保持锁定。'
  } catch (err) {
    const payload = err?.response?.data || err?.data
    probe.value = payload?.probe || probe.value
    error.value = apiErrorMessage(err, '配置保存失败。')
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="config-page">
    <header>
      <h2>存储清理设置</h2>
      <p>一般无需填写，先点“自动识别”；识别失败再用手动配置。</p>
    </header>

    <div v-if="loading" class="notice">正在读取配置…</div>
    <section v-else class="discovery-card">
      <div class="discovery-heading">
        <div>
          <strong>自动识别</strong>
          <span>读取 MoviePilot、qB 和媒体目录；媒体库索引可留空。候选不唯一时不会自动猜。</span>
        </div>
        <div class="discovery-actions">
          <button type="button" :disabled="discovering || saving" @click="discover">
            {{ discovering ? '识别中…' : '自动识别' }}
          </button>
          <button v-if="!advancedOpen" class="secondary" type="button" :disabled="saving" @click="advancedOpen = true">
            手动配置
          </button>
        </div>
      </div>
      <div v-if="discovery" class="discovery-results">
        <div v-for="item in discovery.checks || []" :key="item.key" class="discovery-row">
          <div>
            <span>{{ item.label }}</span>
            <small v-if="item.ambiguous">候选：{{ (item.candidates || []).join('；') }}</small>
          </div>
          <b :class="item.ambiguous ? 'missing' : item.found || item.willCreate ? 'found' : item.optional ? 'optional' : 'missing'">{{ item.ambiguous ? '发现多个候选，需手动选择' : item.found ? '已找到' : item.willCreate ? '将自动创建' : item.optional ? '未配置（可选）' : '需管理员处理' }}</b>
        </div>
        <button
          class="apply-discovery"
          type="button"
          :disabled="saving || !discovery.ready"
          @click="applyDiscovery"
        >
          {{ saving ? '应用中…' : '应用识别结果并验证' }}
        </button>
      </div>
      <div v-if="discoveryError" class="discovery-error">
        <span>{{ discoveryError }} 请改用手动配置。</span>
        <button type="button" :disabled="saving" @click="advancedOpen = true">打开手动配置</button>
      </div>
    </section>

    <section v-if="!loading" class="hr-settings">
      <div class="hr-heading">
        <div>
          <strong>Hit and Run（H&R）</strong>
          <p>默认关闭。开启后，后台会随资源快照刷新，按每个站点配置的入口路径查询；Cookie 和 UA 继续使用 MoviePilot 登录态。</p>
        </div>
        <label class="switch-label">
          <input v-model="form.hit_and_run_enabled" type="checkbox" />
          <span>{{ form.hit_and_run_enabled ? '已开启' : '默认关闭' }}</span>
        </label>
      </div>
      <div class="hr-rows">
        <div v-if="!form.hit_and_run_sites.length" class="notice">尚未配置 H&R 站点；即使总开关打开，H&R 也不会生效。</div>
        <div v-for="(item, index) in form.hit_and_run_sites" :key="`${item.site}-${index}`" class="hr-row">
          <label>
            站点
            <select v-model="item.site">
              <option v-for="site in siteOptions(item.site)" :key="site.site" :value="site.site">{{ site.label || site.site }}</option>
            </select>
          </label>
          <label>
            H&R 列表入口路径
            <input v-model="item.path" autocomplete="off" placeholder="例如：/myhr.php" />
          </label>
          <span class="parser-hint">解析器：NexusPHP 标准</span>
          <button class="remove-hr" type="button" :disabled="saving" @click="removeHitAndRunSite(index)">删除</button>
        </div>
      </div>
      <div class="hr-actions">
        <button type="button" :disabled="saving" @click="addHitAndRunSite">添加站点</button>
        <span v-if="!moviepilotSites.length">当前未读取到 MoviePilot 已启用站点；可保留已有配置，后台会在站点可用后自动测试。</span>
      </div>
    </section>

    <details v-if="!loading" class="advanced-settings" :open="advancedOpen" @toggle="advancedOpen = $event.target.open">
      <summary>手动配置（自动识别失败时使用）</summary>
      <p>从 NAS 文件管理器复制路径；必须是清理台服务能访问到的路径。媒体库索引可以留空。</p>
      <div class="form-grid">
        <label>qBittorrent 地址<input v-model="form.qb_url" autocomplete="off" placeholder="例：http://127.0.0.1:8080" /></label>
        <label>MoviePilot 数据库<input v-model="form.moviepilot_db" autocomplete="off" placeholder="MoviePilot 容器内 user.db 路径" /></label>
        <label>媒体库索引（可选）<input v-model="form.media_index_db" autocomplete="off" placeholder="Jellyfin / Emby 数据库路径，可留空" /></label>
        <label>qB 种子备份目录<input v-model="form.qb_backup" autocomplete="off" placeholder="qB 备份目录" /></label>
        <label>清理事务备份目录<input v-model="form.execution_backup" autocomplete="off" placeholder="清理台可写的备份目录" /></label>
        <label>资源快照有效期（秒）<input v-model.number="form.snapshot_max_age_seconds" type="number" min="300" max="86400" step="60" /></label>
        <label class="wide">允许扫描/清理的根目录（每行一个）<textarea v-model="form.allowed_roots_text" rows="5" placeholder="下载完成目录、电影目录、电视剧目录" /></label>
        <label class="wide">隔离目录映射（每行：卷根目录=隔离目录）<textarea v-model="form.quarantine_roots_text" rows="3" placeholder="例如：/mnt/data=/mnt/data/.storage-cleanup-quarantine" /></label>
      </div>
      <button class="save" :disabled="loading || saving" @click="save">{{ saving ? '保存中…' : '保存手动配置并探测' }}</button>
    </details>

    <div v-if="error" class="error">{{ error }}</div>
    <div v-if="message" class="success">{{ message }}</div>
    <div v-if="probe" class="probe">
      <strong>{{ probe.ok ? '只读探测通过' : '只读探测未通过' }}</strong>
      <span v-if="probe.missing?.length">还有 {{ probe.missing.length }} 项路径未找到，请展开管理员配置查看。</span>
      <span v-if="probe.problems?.length">有 {{ probe.problems.length }} 项安全校验未通过，请展开管理员配置查看。</span>
      <details v-if="probe.missing?.length || probe.problems?.length" class="probe-details">
        <summary>查看管理员诊断</summary>
        <span v-for="item in probe.missing || []" :key="`missing-${item}`">未找到：{{ item }}</span>
        <span v-for="item in probe.problems || []" :key="item">{{ item }}</span>
      </details>
    </div>
  </section>
</template>

<style scoped>
.config-page { display: grid; gap: 18px; padding: 22px; max-width: 980px; }
header { display: grid; gap: 6px; }
h2 { margin: 0; }
p { margin: 0; opacity: .72; line-height: 1.6; }
.discovery-card { display: grid; gap: 12px; padding: 14px 16px; border: 1px solid rgba(var(--v-theme-primary, 59, 130, 246), .24); border-radius: 10px; background: rgba(var(--v-theme-primary, 59, 130, 246), .06); }
.discovery-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.discovery-heading > div { display: grid; gap: 4px; }
.discovery-heading span { opacity: .72; line-height: 1.5; }
.discovery-actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.discovery-heading button, .apply-discovery { padding: 9px 14px; border: 1px solid rgba(var(--v-theme-primary, 59, 130, 246), .35); border-radius: 8px; cursor: pointer; background: transparent; color: inherit; font: inherit; font-weight: 700; }
.discovery-actions .secondary { opacity: .78; }
.discovery-heading button:disabled, .apply-discovery:disabled { opacity: .5; cursor: default; }
.discovery-results { display: grid; gap: 7px; }
.discovery-row { display: flex; justify-content: space-between; gap: 12px; padding: 7px 0; border-top: 1px solid rgba(var(--v-border-color), .16); }
.discovery-row > div { display: grid; gap: 4px; min-width: 0; }
.discovery-row small { opacity: .68; overflow-wrap: anywhere; line-height: 1.45; }
.discovery-row b { font-size: .9em; }
.discovery-row .found { color: #087443; }
.discovery-row .optional { color: #6b7280; }
.discovery-row .missing { color: #b86b11; }
.apply-discovery { justify-self: start; background: rgb(var(--v-theme-primary)); color: rgb(var(--v-theme-on-primary)); }
.discovery-error { display: flex; align-items: center; justify-content: space-between; gap: 12px; color: #b42318; line-height: 1.5; }
.discovery-error button { border: 0; padding: 0; cursor: pointer; background: transparent; color: inherit; font: inherit; font-weight: 700; text-decoration: underline; }
.hr-settings { display: grid; gap: 12px; padding: 14px 16px; border: 1px solid rgba(var(--v-theme-primary, 59, 130, 246), .24); border-radius: 10px; background: rgba(var(--v-theme-primary, 59, 130, 246), .04); }
.hr-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.hr-heading > div { display: grid; gap: 5px; }
.hr-heading p { max-width: 700px; }
.switch-label { display: inline-flex; align-items: center; gap: 8px; white-space: nowrap; font-weight: 700; }
.switch-label input { width: auto; }
.hr-rows { display: grid; gap: 8px; }
.hr-row { display: grid; grid-template-columns: minmax(180px, .8fr) minmax(220px, 1.2fr) auto auto; align-items: end; gap: 10px; padding: 10px 0; border-top: 1px solid rgba(var(--v-border-color), .16); }
.hr-row select, .hr-row input { width: 100%; box-sizing: border-box; padding: 9px 11px; border: 1px solid rgba(var(--v-border-color), .35); border-radius: 8px; background: transparent; color: inherit; font: inherit; font-weight: 400; }
.parser-hint { padding-bottom: 10px; color: #087443; font-size: .88em; white-space: nowrap; }
.hr-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.hr-actions button, .remove-hr { padding: 8px 12px; border: 1px solid rgba(var(--v-theme-primary, 59, 130, 246), .35); border-radius: 8px; cursor: pointer; background: transparent; color: inherit; font: inherit; font-weight: 700; }
.remove-hr { color: #b42318; border-color: rgba(180, 35, 24, .28); }
.hr-actions span { opacity: .72; font-size: .88em; }
.advanced-settings { display: grid; gap: 12px; padding: 2px 0; }
.advanced-settings summary { cursor: pointer; font-weight: 700; }
.advanced-settings > p { padding-left: 2px; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
label { display: grid; gap: 7px; font-weight: 600; }
input, textarea { width: 100%; box-sizing: border-box; padding: 9px 11px; border: 1px solid rgba(var(--v-border-color), .35); border-radius: 8px; background: transparent; color: inherit; font: inherit; font-weight: 400; }
textarea { resize: vertical; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .9em; }
.wide { grid-column: 1 / -1; }
.notice, .error, .success, .probe { display: grid; gap: 5px; padding: 12px 14px; border-radius: 9px; }
.notice { background: rgba(128, 128, 128, .12); }
.error { color: #b42318; background: rgba(180, 35, 24, .1); }
.success { color: #087443; background: rgba(8, 116, 67, .1); }
.probe { background: rgba(32, 106, 255, .08); }
.probe-details { display: grid; gap: 5px; margin-top: 4px; }
.probe-details summary { cursor: pointer; font-weight: 700; }
.save { justify-self: start; padding: 10px 18px; border: 0; border-radius: 8px; cursor: pointer; background: rgb(var(--v-theme-primary)); color: rgb(var(--v-theme-on-primary)); }
.save:disabled { opacity: .5; cursor: default; }
@media (max-width: 760px) { .config-page { padding: 16px; } .form-grid { grid-template-columns: 1fr; } .wide { grid-column: auto; } .discovery-heading { align-items: stretch; flex-direction: column; } .discovery-actions { justify-content: flex-start; } .hr-heading { align-items: stretch; flex-direction: column; } .hr-row { grid-template-columns: 1fr; align-items: stretch; } .parser-hint { padding-bottom: 0; } }
</style>
