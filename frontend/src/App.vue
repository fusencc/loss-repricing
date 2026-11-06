<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ArrowRight, Bell, CircleCheck, Connection, DataAnalysis, Files, Lock, RefreshRight, Setting, SwitchButton, WarningFilled } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import axios from 'axios'
import * as dd from 'dingtalk-jsapi'
import {
  api, display, errorMessage, modeLabel, stateLabel, statuses, timeLabel,
  type Batch, type Contracts, type Identifier, type Item, type ItemStatus,
  type Job, type Overview, type Protection, type Settings,
} from './api'
import ItemTable from './ItemTable.vue'
import ItemDrawer from './ItemDrawer.vue'
import SettingsPanel from './SettingsPanel.vue'
import ErpRatesPanel from './ErpRatesPanel.vue'
import ErpSourcePanel from './ErpSourcePanel.vue'
import ProtectionPanel from './ProtectionPanel.vue'
import CoordinationPanel from './CoordinationPanel.vue'
import ScopesPanel from './ScopesPanel.vue'
import NotificationsPanel from './NotificationsPanel.vue'
import type { Principal } from './api'

const pages = [
  { id: 'overview', label: '运行概览', icon: DataAnalysis, description: '查看当前环境、处理进度和最近批次。' },
  { id: 'batches', label: '运行批次', icon: Files, description: '按业务日期查看批次、商品结果和计算依据。' },
  { id: 'exceptions', label: '异常处理', icon: Bell, description: '查看待处理和待核验商品，按需要重新评估。' },
  { id: 'notifications', label: '通知收件箱', icon: Bell, description: '查看范围内通知，确认业务处理，区分投递与价格生效。' },
  { id: 'coordination', label: '账号与归属', icon: Lock, description: '配置独立账号、负责范围与自动托管授权。' },
  { id: 'settings', label: '运行配置', icon: Setting, description: '管理暂停开关、试点范围和定时检查。' },
  { id: 'protections', label: '保护名单', icon: Lock, description: '按店铺、站点和主 SKU 管理人工保护。' },
] as const
type Page = typeof pages[number]['id']
function route(): Page {
  const hash = window.location.hash.slice(1)
  return pages.find(entry => entry.id === hash)?.id ?? 'overview'
}

const page = ref<Page>(route())
const currentPage = computed(() => pages.find(entry => entry.id === page.value)!)
const username = ref('')
const principal = ref<Principal | null>(null)
const isAdmin = computed(() => principal.value?.role === 'admin')
const visiblePages = computed(() => pages.filter(entry => isAdmin.value || !['settings', 'protections', 'coordination'].includes(entry.id)))
const dingConfig = ref<{ enabled?: boolean; configured?: boolean; corp_id?: string; client_id?: string; message?: string }>({})
async function setPrincipal() { const { data } = await api.get<Principal>('/auth/me'); principal.value = data; username.value = data.username; if (!visiblePages.value.some(p => p.id === page.value)) page.value = 'overview' }
async function dingLogin() {
  loginBusy.value = true; loginError.value = ''
  try {
    if (dd.env.platform === 'notInDingTalk') throw new Error('请在钉钉内打开此页面，或使用独立账号登录。')
    const code = await new Promise<string>((resolve, reject) => dd.requestAuthCode({ corpId: dingConfig.value.corp_id || '', clientId: dingConfig.value.client_id || '', onSuccess: (r: { code?: string; authCode?: string }) => resolve(r.code || r.authCode || ''), onFail: reject }))
    await api.post('/auth/dingtalk', { code }); await setPrincipal(); await loadPage()
  } catch (error) { loginError.value = errorMessage(error) } finally { loginBusy.value = false }
}
const initialized = ref(false)
const credentials = ref({ username: '', password: '' })
const loginError = ref('')
const loginBusy = ref(false)
const healthMode = ref('')
const overview = ref<Overview | null>(null)
const batches = ref<Batch[]>([])
const activeBatch = ref<(Batch & { items: Item[] }) | null>(null)
const items = ref<Item[]>([])
const itemFilter = ref<ItemStatus | ''>('待处理')
const settings = ref<Settings | null>(null)
const contracts = ref<Contracts | null>(null)
const protections = ref<Protection[]>([])
const loading = ref(false)
const pageError = ref('')
const pollError = ref('')
const logoutBusy = ref(false)
const drawerOpen = ref(false)
const item = ref<Item | null>(null)
const itemLoading = ref(false)
const itemError = ref('')
const activeJob = ref<Job | null>(null)
const actionBusy = ref(false)
const actionLabel = ref('')
const actionError = ref('')
const jobMessage = ref('')
const jobProblem = ref(false)
let pageRequest = 0
let detailRequest = 0
let jobChecking = false
let polling = false
let interval: number | undefined

const mode = computed(() => overview.value?.mode || healthMode.value)
const modeCopy = computed(() => ({
  demo: '使用演示样本运行完整流程。',
  file: '读取文件快照并对账。',
  live: '数据源待接入。真实提交需要完成数据接入，并启用对应的试点范围。',
}[mode.value] ?? '正在读取当前运行环境。'))
const statusCards = [
  { label: '本轮不调', color: 'slate', hint: '按当前规则无需调价' },
  { label: '待处理', color: 'amber', hint: '需补齐数据或重新评估' },
  { label: '待核验', color: 'blue', hint: '等待逐行结果或有效价回读' },
  { label: '已核验', color: 'teal', hint: '有效价已完成核验' },
] as const

const interceptor = api.interceptors.response.use(response => response, (error: unknown) => {
  if (axios.isAxiosError(error) && error.response?.status === 401 && username.value) {
    endSession()
    loginError.value = '登录已失效，请重新登录。'
  }
  return Promise.reject(error)
})

function endSession() {
  username.value = ''
  principal.value = null
  overview.value = null
  batches.value = []
  items.value = []
  activeBatch.value = null
  settings.value = null
  contracts.value = null
  protections.value = []
  activeJob.value = null
  jobMessage.value = ''
  actionError.value = ''
  pollError.value = ''
  pageError.value = ''
  ++pageRequest
  closeItem()
}
async function init() {
  const health = api.get<{ mode: string }>('/health')
    .then(response => { healthMode.value = response.data.mode })
    .catch(() => undefined)
  try {
    await setPrincipal()
  } catch (error) {
    if (!axios.isAxiosError(error) || error.response?.status !== 401) loginError.value = errorMessage(error)
  } finally {
    initialized.value = true
  }
  try { dingConfig.value = (await api.get('/auth/dingtalk-config')).data } catch { /* 本地账号仍可登录 */ }
  await health
  if (username.value) await loadPage()
}
async function login() {
  if (loginBusy.value || !credentials.value.username.trim() || !credentials.value.password) return
  loginError.value = ''
  loginBusy.value = true
  try {
    await api.post('/auth/login', {
      username: credentials.value.username.trim(), password: credentials.value.password,
    })
    await setPrincipal()
    credentials.value.password = ''
    await loadPage()
  } catch (error) {
    loginError.value = errorMessage(error)
  } finally {
    loginBusy.value = false
  }
}
async function logout() {
  if (logoutBusy.value) return
  logoutBusy.value = true
  try {
    await api.post('/auth/logout')
    endSession()
  } catch (error) {
    if (username.value) ElMessage.error(errorMessage(error))
  } finally {
    logoutBusy.value = false
  }
}

function navigate(target: Page) {
  if (page.value === target) return
  window.location.hash = target
}
function showStatus(status: ItemStatus) {
  itemFilter.value = status
  navigate('exceptions')
}
function onRoute() {
  const target = route()
  page.value = principal.value && !visiblePages.value.some(p => p.id === target) ? 'overview' : target
}
watch(page, () => {
  activeBatch.value = null
  closeItem()
  if (username.value) void loadPage()
})
watch(itemFilter, () => { if (page.value === 'exceptions' && username.value) void loadPage() })

async function loadOverview() {
  const { data } = await api.get<Overview>('/overview')
  if (username.value) overview.value = data
}
async function loadPage() {
  const currentRequest = ++pageRequest
  const targetPage = page.value
  loading.value = true
  pageError.value = ''
  pollError.value = ''
  try {
    if (targetPage === 'overview') {
      await loadOverview()
    } else if (targetPage === 'batches') {
      const { data } = await api.get<Batch[]>('/batches')
      if (currentRequest === pageRequest) batches.value = data
    } else if (targetPage === 'exceptions') {
      const { data } = await api.get<Item[]>('/items', { params: { status: itemFilter.value || undefined } })
      if (currentRequest === pageRequest) items.value = data
    } else if (targetPage === 'settings') {
      const [configuration, readiness] = await Promise.all([
        api.get<Settings>('/settings'),
        api.get<Contracts>('/contracts'),
      ])
      if (currentRequest === pageRequest) {
        settings.value = configuration.data
        contracts.value = readiness.data
      }
    } else if (targetPage === 'protections' && isAdmin.value) {
      const { data } = await api.get<Protection[]>('/protections')
      if (currentRequest === pageRequest) protections.value = data
    }
    if (!overview.value && targetPage !== 'overview') await loadOverview()
  } catch (error) {
    if (currentRequest === pageRequest && username.value) pageError.value = errorMessage(error)
  } finally {
    if (currentRequest === pageRequest) loading.value = false
  }
}

async function openBatch(id: Identifier) {
  const currentRequest = ++pageRequest
  loading.value = true
  pageError.value = ''
  try {
    const { data } = await api.get<Batch & { items: Item[] }>(`/batches/${encodeURIComponent(id)}`)
    if (currentRequest === pageRequest) activeBatch.value = data
  } catch (error) {
    if (currentRequest === pageRequest && username.value) pageError.value = errorMessage(error)
  } finally {
    if (currentRequest === pageRequest) loading.value = false
  }
}
function closeBatch() {
  activeBatch.value = null
  void loadPage()
}
async function openLatest() {
  const id = overview.value?.latest_batch?.id
  if (id == null) return
  navigate('batches')
  page.value = 'batches'
  await nextTick()
  await openBatch(id)
}
async function openItem(row: Item) {
  const currentRequest = ++detailRequest
  item.value = row
  itemError.value = ''
  itemLoading.value = true
  drawerOpen.value = true
  try {
    const { data } = await api.get<Item>(`/items/${encodeURIComponent(row.id)}`)
    if (currentRequest === detailRequest) item.value = data
  } catch (error) {
    if (currentRequest === detailRequest && username.value) itemError.value = errorMessage(error)
  } finally {
    if (currentRequest === detailRequest) itemLoading.value = false
  }
}
function closeItem() {
  ++detailRequest
  itemLoading.value = false
  drawerOpen.value = false
  item.value = null
}

const jobDone = new Set(['done', 'completed', 'succeeded', 'success', 'failed', 'error', 'cancelled'])
async function runJob(path: string, label: string, body?: object) {
  if (actionBusy.value || activeJob.value) return
  actionBusy.value = true
  actionLabel.value = label
  actionError.value = ''
  jobMessage.value = ''
  try {
    const { data } = await api.post<{ job_id: Identifier }>(path, body)
    activeJob.value = { id: data.job_id, state: 'queued', batch_id: null, error: null }
    await checkJob()
  } catch (error) {
    if (username.value) actionError.value = errorMessage(error)
  } finally {
    actionBusy.value = false
  }
}
async function checkJob() {
  if (!activeJob.value || jobChecking || document.hidden) return
  jobChecking = true
  try {
    const { data } = await api.get<Job>(`/jobs/${encodeURIComponent(activeJob.value.id)}`)
    if (!username.value) return
    actionError.value = ''
    activeJob.value = data
    if (jobDone.has(data.state.toLowerCase())) {
      jobProblem.value = ['failed', 'error', 'cancelled'].includes(data.state.toLowerCase())
      jobMessage.value = `${actionLabel.value}${stateLabel(data.state)}${data.batch_id == null ? '' : ` · 批次 #${data.batch_id}`}${data.error ? ` · ${data.error}` : ''}`
      activeJob.value = null
      if (page.value === 'batches' && activeBatch.value) await openBatch(activeBatch.value.id)
      else if (page.value !== 'settings' && page.value !== 'protections') await loadPage()
      if (item.value) await openItem(item.value)
    }
  } catch (error) {
    if (username.value) actionError.value = errorMessage(error)
  } finally {
    jobChecking = false
  }
}
function reevaluate(id: Identifier, note: string) {
  void runJob(`/items/${encodeURIComponent(id)}/reevaluate`, '重新评估', { note: note.trim() || undefined })
}

async function poll() {
  if (document.hidden || !username.value || polling) return
  polling = true
  try {
    const currentRequest = pageRequest
    const filteredItems = page.value === 'exceptions' && !loading.value
      ? api.get<Item[]>('/items', { params: { status: itemFilter.value || undefined } })
      : null
    const results = await Promise.allSettled([loadOverview(), filteredItems, checkJob()])
    if (currentRequest === pageRequest && username.value) {
      const [overviewResult, itemsResult] = results
      if (itemsResult.status === 'fulfilled' && itemsResult.value) items.value = itemsResult.value.data
      const failure = [overviewResult, itemsResult].find(result => result.status === 'rejected')
      pollError.value = failure ? `自动刷新失败 · ${errorMessage(failure.reason)}` : ''
    }
  } finally {
    polling = false
  }
}

async function saveSettings(value: Partial<Settings>) {
  const { data } = await api.patch<Settings>('/settings', value)
  settings.value = data
  if (overview.value) overview.value.settings = data
  ElMessage.success('运行配置已保存')
}
async function addProtection(value: Pick<Protection, 'shop_id' | 'site' | 'main_sku' | 'reason'>) {
  await api.post('/protections', value)
  await loadPage()
  ElMessage.success('保护条目已添加')
}
async function removeProtection(entry: Protection) {
  await ElMessageBox.confirm(`停用 ${entry.shop_id} / ${entry.site} / ${entry.main_sku} 的人工保护？`, '停用保护', {
    confirmButtonText: '停用保护', cancelButtonText: '取消', type: 'warning',
  })
  await api.delete(`/protections/${encodeURIComponent(entry.id)}`)
  await loadPage()
  ElMessage.success('保护条目已停用')
}

onMounted(() => {
  void init()
  window.addEventListener('hashchange', onRoute)
  document.addEventListener('visibilitychange', poll)
  interval = window.setInterval(poll, 10000)
})
onUnmounted(() => {
  api.interceptors.response.eject(interceptor)
  window.removeEventListener('hashchange', onRoute)
  document.removeEventListener('visibilitychange', poll)
  window.clearInterval(interval)
})
</script>

<template>
  <main v-if="!initialized" class="login-shell"><p class="startup">正在连接运营工作台…</p></main>
  <main v-else-if="!username" class="login-shell">
    <div class="login-card">
      <div class="brand-mark"><el-icon><DataAnalysis /></el-icon></div>
      <p class="eyebrow">电商运营 · 销售协作入口</p>
      <h1>亏损补亏工作台</h1>
      <p class="muted">登录后查看调价批次、处理异常和核验结果。</p>
      <span v-if="mode" class="mode-pill" :class="`mode-${mode}`">{{ modeLabel(mode) }}</span>
      <el-form class="login-form" label-position="top" @submit.prevent="login">
        <el-form-item label="账号"><el-input v-model="credentials.username" autocomplete="username" placeholder="请输入账号" /></el-form-item>
        <el-form-item label="密码"><el-input v-model="credentials.password" type="password" autocomplete="current-password" placeholder="请输入密码" show-password /></el-form-item>
        <el-alert v-if="loginError" :title="loginError" type="error" show-icon :closable="false" />
        <el-button class="login-button" type="primary" native-type="submit" :loading="loginBusy" :disabled="!credentials.username.trim() || !credentials.password">登录工作台</el-button>
      </el-form>
      <el-button class="login-button" :disabled="!dingConfig.enabled || loginBusy" @click="dingLogin">钉钉账号登录</el-button>
      <p class="muted">{{ dingConfig.message || (dingConfig.enabled ? '已绑定的账号可在钉钉内免登。' : '钉钉免登未配置；请使用分配的独立账号。') }}</p>
    </div>
  </main>

  <div v-else class="app-shell">
    <aside class="sidebar">
      <a href="#overview" class="brand">
        <span class="brand-mark"><el-icon><DataAnalysis /></el-icon></span>
        <span><strong>亏损补亏</strong><small>运营工作台</small></span>
      </a>
      <p class="nav-label">运营管理</p>
      <nav aria-label="运营管理">
        <a v-for="entry in visiblePages" :key="entry.id" :href="`#${entry.id}`" :aria-current="page === entry.id ? 'page' : undefined" :class="{ selected: page === entry.id }">
          <el-icon><component :is="entry.icon" /></el-icon><span>{{ entry.label }}</span>
        </a>
      </nav>
      <div class="sidebar-footer"><span class="footer-dot"></span>内部管理系统</div>
    </aside>

    <div class="workspace">
      <header class="topbar">
        <div class="breadcrumb"><span>运营工作台</span><el-icon><ArrowRight /></el-icon><strong>{{ currentPage.label }}</strong></div>
        <div class="topbar-actions">
          <span v-if="mode" class="mode-pill" :class="`mode-${mode}`"><span class="mode-dot"></span>{{ modeLabel(mode) }}</span>
          <span class="user-name">{{ principal?.display_name || username }} · {{ { admin: '管理员', manager: '主管', sales: '销售员' }[principal?.role || 'sales'] }}</span>
          <el-button text :icon="SwitchButton" :loading="logoutBusy" @click="logout">退出</el-button>
        </div>
      </header>
      <main class="workspace-body">
        <div class="page-heading">
          <div><h1>{{ currentPage.label }}</h1><p class="muted">{{ currentPage.description }}</p></div>
          <el-button :icon="RefreshRight" :loading="loading" @click="activeBatch ? openBatch(activeBatch.id) : loadPage()">刷新</el-button>
        </div>
        <div v-if="pageError || pollError || actionError || activeJob || jobMessage" class="alerts">
          <el-alert v-if="pageError" :title="pageError" type="error" show-icon :closable="false" />
          <el-alert v-if="pollError" :title="pollError" type="warning" show-icon :closable="false" />
          <el-alert v-if="actionError" :title="actionError" type="error" show-icon @close="actionError = ''" />
          <el-alert v-if="activeJob" :title="`${actionLabel} · ${stateLabel(activeJob.state)} · 任务 #${activeJob.id}`" type="info" show-icon :closable="false" />
          <el-alert v-else-if="jobMessage" :title="jobMessage" :type="jobProblem ? 'error' : 'success'" show-icon @close="jobMessage = ''" />
        </div>

        <div v-if="page === 'overview'" v-loading="loading" class="page-content">
          <section class="hero">
            <div>
              <p class="eyebrow"><el-icon><Connection /></el-icon> 当前运行环境</p>
              <h2>{{ mode ? modeLabel(mode) : '正在读取环境' }}</h2>
              <p>{{ modeCopy }}</p>
              <div v-if="overview && isAdmin" class="hero-badges">
                <span v-if="overview.settings.paused" class="hero-badge warning">全局已暂停</span>
                <span v-else class="hero-badge">{{ overview.settings.autonomous_enabled ? '自动运行已开启' : '自动运行未开启' }}</span>
                <span class="hero-badge">{{ overview.settings.schedule_enabled ? '定时检查已开启' : '定时检查未开启' }}</span>
                <span v-if="overview.delayed" class="hero-badge warning">数据检查已延迟</span>
              </div>
            </div>
            <div v-if="isAdmin" class="hero-actions">
              <el-button v-if="mode === 'demo'" type="primary" :loading="actionBusy && actionLabel === '运行演示批次'" :disabled="actionBusy || !!activeJob" @click="runJob('/demo/run', '运行演示批次')">运行演示批次</el-button>
              <el-button v-else-if="mode === 'file'" type="primary" :loading="actionBusy && actionLabel === '启动批次'" :disabled="actionBusy || !!activeJob" @click="runJob('/batches/run', '启动批次')">启动批次</el-button>
              <el-button plain :disabled="actionBusy || !!activeJob" @click="runJob('/reconcile', '回读核验')">回读核验</el-button>
            </div>
          </section>
          <ScopesPanel v-if="!isAdmin" />
          <section class="stats" aria-label="商品处理状态">
            <button v-for="card in statusCards" :key="card.label" class="stat-card" type="button" @click="showStatus(card.label)">
              <span class="stat-label"><span :class="`stat-dot ${card.color}`"></span>{{ card.label }}<el-icon><ArrowRight /></el-icon></span>
              <strong>{{ overview?.counts[card.label] ?? '—' }}</strong>
              <span class="stat-hint">{{ card.hint }}</span>
            </button>
          </section>
          <div class="overview-grid">
            <section class="panel overview-panel">
              <header class="panel-heading"><h2>最近批次</h2><el-button type="primary" link @click="navigate('batches')">全部批次 <el-icon><ArrowRight /></el-icon></el-button></header>
              <div v-if="overview?.latest_batch">
                <div class="featured-row">
                  <div><p class="eyebrow">批次 #{{ overview.latest_batch.id }}</p><strong>{{ overview.latest_batch.business_date }}</strong></div>
                  <el-tag effect="plain">{{ stateLabel(overview.latest_batch.status) }}</el-tag>
                </div>
                <p class="muted">创建时间 {{ timeLabel(overview.latest_batch.created_at) }}</p>
                <el-alert v-if="overview.latest_batch.error" :title="overview.latest_batch.error" type="error" :closable="false" />
                <el-button type="primary" link @click="openLatest">查看批次详情</el-button>
              </div>
              <p v-else class="empty-inline">{{ overview ? '暂无运行批次。' : '等待批次数据。' }}</p>
              <div class="panel-footer">累计批次 <strong>{{ overview?.batch_count ?? '—' }}</strong></div>
            </section>
            <section v-if="isAdmin" class="panel overview-panel">
              <header class="panel-heading"><h2>执行就绪状态</h2><el-button v-if="isAdmin" type="primary" link @click="navigate('settings')">查看配置 <el-icon><ArrowRight /></el-icon></el-button></header>
              <dl v-if="overview" class="readiness-list">
                <div><dt>真实提交开关</dt><dd>{{ overview.readiness.live_enabled ? '已开启' : '未开启' }}</dd></div>
                <div><dt>数据接口就绪</dt><dd>{{ overview.readiness.contract_ready ? '已就绪' : '待补齐' }}</dd></div>
                <div><dt>后台任务最近运行</dt><dd>{{ timeLabel(overview.worker.heartbeat) }}</dd></div>
              </dl>
              <p v-if="overview?.worker.last_error" class="service-error"><el-icon><WarningFilled /></el-icon>{{ overview.worker.last_error }}</p>
              <ul v-if="overview?.readiness.missing.length" class="missing-list">
                <li v-for="entry in overview.readiness.missing" :key="entry">{{ entry }}</li>
              </ul>
              <p v-if="overview?.readiness.contract_ready" class="service-ready"><el-icon><CircleCheck /></el-icon> 已读取就绪状态</p>
            </section>
          </div>
        </div>

        <div v-else-if="page === 'batches'" v-loading="loading" class="page-content">
          <section v-if="activeBatch" class="panel table-panel">
            <div class="table-toolbar">
              <div><el-button type="primary" link @click="closeBatch">返回批次列表</el-button><h2>批次 #{{ activeBatch.id }}</h2><p class="muted">{{ activeBatch.business_date }} · {{ modeLabel(activeBatch.mode) }} · {{ stateLabel(activeBatch.status) }}</p></div>
            </div>
            <el-alert v-if="activeBatch.error" :title="activeBatch.error" type="error" show-icon :closable="false" />
            <ItemTable :items="activeBatch.items" empty-text="该批次没有商品记录" @select="openItem" />
          </section>
          <section v-else class="panel table-panel">
            <div class="table-toolbar"><div><h2>批次记录</h2><p class="muted">业务日期与创建时间以北京时间显示</p></div></div>
            <el-table :data="batches" row-key="id" empty-text="暂无运行批次">
              <el-table-column prop="id" label="批次 ID" min-width="110" />
              <el-table-column prop="business_date" label="业务日期" min-width="115" />
              <el-table-column label="运行环境" min-width="115"><template #default="{ row }"><span class="mode-pill" :class="`mode-${row.mode}`">{{ modeLabel(row.mode) }}</span></template></el-table-column>
              <el-table-column label="状态" min-width="115"><template #default="{ row }">{{ stateLabel(row.status) }}</template></el-table-column>
              <el-table-column label="创建时间" min-width="176"><template #default="{ row }">{{ timeLabel(row.created_at) }}</template></el-table-column>
              <el-table-column label="异常说明" min-width="170" show-overflow-tooltip><template #default="{ row }">{{ display(row.error) }}</template></el-table-column>
              <el-table-column label="操作" width="88"><template #default="{ row }"><el-button type="primary" link @click="openBatch(row.id)">查看批次</el-button></template></el-table-column>
            </el-table>
          </section>
        </div>

        <section v-else-if="page === 'exceptions'" class="panel table-panel">
          <div class="table-toolbar">
            <div><h2>商品记录</h2><p class="muted">打开详情查看证据，待处理商品可重新评估</p></div>
            <label class="inline-filter"><span>处理状态</span>
              <el-select v-model="itemFilter" aria-label="处理状态">
                <el-option label="全部状态" value="" />
                <el-option v-for="status in statuses" :key="status" :label="status" :value="status" />
              </el-select>
            </label>
          </div>
          <ItemTable :items="items" :loading="loading" @select="openItem" />
        </section>

        <NotificationsPanel v-else-if="page === 'notifications'" :admin="isAdmin" @select="openItem" />
        <CoordinationPanel v-else-if="page === 'coordination' && isAdmin" />
        <div v-else-if="page === 'settings' && isAdmin" v-loading="loading" class="page-content">
          <ErpSourcePanel />
          <ErpRatesPanel />
          <SettingsPanel v-if="settings && contracts" :settings="settings" :contracts="contracts" :live-enabled="overview?.readiness.live_enabled ?? false" :save="saveSettings" />
        </div>

        <ProtectionPanel v-else-if="page === 'protections' && isAdmin" :protections="protections" :loading="loading" :add="addProtection" :remove="removeProtection" />
      </main>
    </div>
    <ItemDrawer :actor-role="principal?.role || 'sales'" :actor-id="principal?.id || ''" :admin="isAdmin" :open="drawerOpen" :item="item" :loading="itemLoading" :error="itemError" :busy="actionBusy || !!activeJob || itemLoading" @close="closeItem" @refresh="item && openItem(item)" @reevaluate="reevaluate" />
  </div>
</template>




