<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { Download, Search } from '@element-plus/icons-vue'
import { ElDatePicker } from 'element-plus'
import { timeLabel } from './api'
import { connectRecords } from './records-live'
import { groupBySeller, recordTone, countRecordStatuses, recordDisplayStates, recordDepartments, initialRecordDepartment,
  filterRecordBusinessDates, todayBusinessDateRange, type BusinessDateRange, type RecordData, type SellerGroup } from './records-model'
import { formatDecimal } from './decimal-display'
import { downloadSellerExcel } from './records-download'
import SellerRecordLogs from './SellerRecordLogs.vue'
import RecordsDots from './RecordsDots.vue'
import repricingLogo from './assets/repricing-logo.png'
import './records.css'

defineProps<{ mode: string }>()
const departmentPreferenceKey = 'loss-repricing:records-department'
const requestedDepartment = new URLSearchParams(window.location.search).get('department')
let savedDepartment: string | null = null
try {
  savedDepartment = window.localStorage.getItem(departmentPreferenceKey)
} catch { /* 浏览器禁用本地存储时仍可正常查看。 */ }
const department = ref(initialRecordDepartment(requestedDepartment, savedDepartment))
const departments = recordDepartments
const snapshots = new Map<string, RecordData>()
const data = shallowRef<RecordData | null>(null)
const error = ref('')
const loading = computed(() => !data.value && !error.value)
const refreshing = ref(false)
const downloadError = ref('')
const dialogOpen = ref(false)
const logTitle = computed(() => `${activeSeller.value?.name ?? '销售员'}的提价日志`)
const selectedSeller = ref<string | null>(null)
const sellerQuery = ref('')
const searchInput = ref<HTMLInputElement | null>(null)
const todayRange = ref(todayBusinessDateRange())
const todayItems = computed(() => filterRecordBusinessDates(data.value?.items ?? [], todayRange.value))
const groups = computed(() => groupBySeller(data.value?.items ?? [], todayRange.value))
const counts = computed(() => countRecordStatuses(todayItems.value))
// 仅筛选当前部门已读取的姓名；保留完整人员记录，不改变部门汇总或下载内容。
const visibleGroups = computed(() => {
  const query = sellerQuery.value.trim().toLocaleLowerCase()
  return query ? groups.value.filter(group => group.name.toLocaleLowerCase().includes(query)) : groups.value
})
const activeSeller = computed(() => groups.value.find(group => group.id === selectedSeller.value))
const logRange = ref(todayBusinessDateRange())
const logItems = computed(() => filterRecordBusinessDates(activeSeller.value?.items ?? [], logRange.value))
const departmentName = computed(() => departments.find(entry => entry.id === department.value)?.name ?? '部门提价记录')
const sellerCount = computed(() => groupBySeller(todayItems.value).filter(group => group.assigned).length)
const states = recordDisplayStates
let dayTimer: ReturnType<typeof setInterval> | undefined
let disconnect: (() => void) | undefined

function updateToday() {
  const range = todayBusinessDateRange()
  if (range.start !== todayRange.value.start) todayRange.value = range
}
let request = 0
let opener: HTMLElement | null = null

function stopLive() {
  ++request
  disconnect?.()
  disconnect = undefined
  refreshing.value = false
}

function startLive() {
  updateToday()
  stopLive()
  if (document.visibilityState !== 'visible') return
  const current = request
  const id = department.value
  error.value = ''
  refreshing.value = data.value !== null
  disconnect = connectRecords<RecordData>(id, response => {
    // 旧连接或不属于该部门的消息不能写入显示和快照。
    if (current !== request || response.department !== id) return
    snapshots.set(id, response)
    data.value = response
    refreshing.value = false
    error.value = ''
    if (dialogOpen.value && !activeSeller.value) dialogOpen.value = false
  }, () => {
    if (current !== request) return
    refreshing.value = false
    error.value = '实时连接中断，正在重新连接；当前记录可能不是最新状态'
  })
}

function onVisibilityChange() {
  if (document.visibilityState === 'visible') startLive()
  else stopLive()
}

function changeDepartment(id: string) {
  if (department.value === id) return
  department.value = id
  try {
    window.localStorage.setItem(departmentPreferenceKey, id)
  } catch { /* 本地存储不可用不影响切换。 */ }
  dialogOpen.value = false
  selectedSeller.value = null
  sellerQuery.value = ''
  data.value = snapshots.get(id) ?? null
  error.value = ''
  opener = null
  startLive()
}

function openSeller(group: SellerGroup, event: MouseEvent) {
  const target = event.currentTarget as HTMLElement
  opener = target instanceof HTMLButtonElement ? target : target.querySelector<HTMLButtonElement>('.records-person-button')
  opener?.focus()
  selectedSeller.value = group.id
  logRange.value = todayBusinessDateRange()
  downloadError.value = ''
  dialogOpen.value = true
}

function changeLogDate(boundary: keyof BusinessDateRange, value: string) {
  const { start, end } = logRange.value
  logRange.value = boundary === 'start'
    ? { start: value, end: value > end ? value : end }
    : { start: value < start ? value : start, end: value }
  downloadError.value = ''
}

async function downloadSeller(group: Pick<SellerGroup, 'name' | 'items'>, range?: BusinessDateRange) {
  downloadError.value = ''
  try {
    await downloadSellerExcel(group, departmentName.value, range)
  } catch {
    downloadError.value = 'Excel 下载未成功，请重试。'
  }
}

function restoreFocus() {
  void nextTick(() => { if (opener?.isConnected) opener.focus() })
}

onMounted(() => {
  startLive()
  dayTimer = setInterval(updateToday, 30_000)
  document.addEventListener('visibilitychange', onVisibilityChange)
})
onUnmounted(() => {
  stopLive()
  clearInterval(dayTimer)
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<template>
  <div class="records-page">
    <div class="records-aura" aria-hidden="true"></div>
    <RecordsDots />
    <main class="records-shell">
      <header class="records-header motion-page">
        <div>
          <div class="records-title"><img class="records-mark" :src="repricingLogo" alt="" aria-hidden="true" /><h1>提价日志</h1></div>
        </div>
        <div class="records-header-end">
          <nav class="records-departments" aria-label="切换部门"><button v-for="entry in departments" :key="entry.id" type="button" :aria-pressed="department === entry.id" :class="{ active: department === entry.id }" aria-controls="records-list" @click="changeDepartment(entry.id)">{{ entry.name }}</button></nav>
        </div>
      </header>

      <div class="records-overview" aria-live="polite">
        <div>
          <strong>{{ departmentName }}</strong>
          <span v-if="data">今日 {{ sellerCount }} 位销售员，共 {{ todayItems.length }} 条记录<span v-if="todayItems.some(item => item.seller.id === null)">，含待分配记录</span></span>
          <span v-if="refreshing" class="records-refreshing">正在更新，当前显示上次读取的结果</span>
        </div>
        <div v-if="data" class="records-counts"><span v-for="state in states" :key="state"><i :class="recordTone(state)"></i>{{ state === '不调价' ? '不调价' : state }} <b>{{ counts[state] }}</b></span><span class="records-activity-placeholder"><i class="neutral"></i>活动中</span></div>
      </div>
      <p v-if="error" class="records-error motion-reveal" role="alert"><strong>{{ data ? '更新未成功，以下保留最近读取的数据。' : '暂时无法读取记录。' }}</strong>{{ error }}。可切换部门或刷新页面，系统也会自动重试。</p>

      <p v-if="downloadError" class="records-error" role="alert">{{ downloadError }}</p>
      <section id="records-list" class="records-glass" :aria-busy="loading">
        <div class="records-panel-heading">
          <h2>销售员提价记录</h2>
          <div class="records-search" role="search">
            <input id="records-seller-search" ref="searchInput" v-model="sellerQuery"
              type="search" placeholder="搜索当前部门销售员" aria-label="搜索当前部门销售员" />
            <button type="button" class="records-download" aria-label="聚焦销售员搜索框" @click="searchInput?.focus()">
              <Search aria-hidden="true" />
            </button>
          </div>
        </div>
        <div v-if="loading" class="records-empty records-loading" role="status"><h3>正在读取部门日志</h3><p>记录读取后会在这里完整展示。</p></div>
        <div v-else-if="!data || !groups.length" class="records-empty motion-reveal"><h3>{{ data ? '暂无提价记录' : '记录暂未加载' }}</h3><p>{{ data ? '部门产生提价处理记录后，会按销售员展示在这里。' : '恢复连接后会自动重试。' }}</p></div>
        <div v-else-if="!visibleGroups.length" class="records-empty" role="status">
          <h3>未找到销售员</h3><p>请换一个姓名关键词。</p>
        </div>
        <table v-else class="records-table motion-reveal">
          <caption class="records-sr">{{ departmentName }}全部销售员提价记录，点击姓名查看本人详细日志</caption>
          <thead><tr><th scope="col">销售员 / 全部记录</th><th scope="col">最近已提价商品 / SKU</th><th scope="col">店铺 / 站点</th><th scope="col" title="算价选中的订单修正一次毛利，按原币展示">修正一次毛利</th><th scope="col">最近营销价变动</th><th scope="col">上传价</th><th scope="col">今日处理状态</th><th scope="col">最近更新</th></tr></thead>
          <tbody><tr v-for="(group, index) in visibleGroups" :key="group.id" class="records-person-row" :data-seller="group.id" @click="openSeller(group, $event)">
            <td class="records-person-cell"><button class="records-person-button" :aria-label="`查看${group.name}的全部${group.items.length}条提价日志`" aria-haspopup="dialog" @click.stop="openSeller(group, $event)"><span class="records-avatar" :class="`tone-${index % 4}`" aria-hidden="true">{{ group.assigned ? group.name.slice(-1) : '?' }}</span><span><strong>{{ group.name }}</strong><small>共计{{ group.items.length }}条</small></span></button></td>
            <template v-if="group.latestPriced">
              <td class="records-product-cell"><strong>{{ group.latestPriced.key.sku || 'SKU 未记录' }}</strong><small>商品 {{ group.latestPriced.key.product_id || group.latestPriced.key.item_id || '未记录' }}</small></td>
              <td class="records-shop-cell"><strong>{{ group.latestPriced.shop_name || group.latestPriced.key.shop_id || '店铺未记录' }}</strong><small>{{ group.latestPriced.key.site }} / {{ group.latestPriced.key.platform }}</small></td>
              <td class="records-margin-cell"><span class="records-mobile-label">修正一次毛利</span><strong>{{ formatDecimal(group.latestPriced.first_margin?.amount ?? '未记录') }}</strong><small v-if="group.latestPriced.first_margin">{{ group.latestPriced.first_margin.currency }}</small></td>
              <td class="records-price-cell"><span class="records-mobile-label">最近营销价变动</span><div class="records-price-change"><span>{{ formatDecimal(group.latestPriced.decision?.current_actual ?? '未记录') }}</span><i aria-hidden="true">→</i><strong>{{ formatDecimal(group.latestPriced.execution?.actual_price ?? group.latestPriced.decision?.target_actual ?? '未记录') }}</strong></div><small>{{ group.latestPriced.execution?.actual_price == null ? '目标 · ' : '' }}{{ group.latestPriced.decision?.trace.station_currency || '币种未记录' }}</small></td>
              <!-- 上传价只取本条记录的提交金额，不用建议价或回读价替代。 -->
              <td class="records-upload-cell"><span class="records-mobile-label">上传价</span><strong>{{ formatDecimal(group.latestPriced.execution?.uploaded_price ?? '未记录') }}</strong><small v-if="group.latestPriced.execution?.uploaded_price != null">{{ group.latestPriced.decision?.trace.station_currency || '币种未记录' }}</small></td>
            </template>
            <td v-else class="records-no-priced" colspan="5">暂无已提价记录</td>
            <td class="records-progress-cell"><div class="records-status-list"><template v-for="state in states" :key="state"><span v-if="group.counts[state]" class="records-status" :class="recordTone(state)"><i></i>{{ state }} {{ group.counts[state] }}</span></template><span v-if="!states.some(state => group.counts[state])" class="records-status neutral">当天无记录</span></div></td>
            <td class="records-time-cell"><div class="records-row-end"><time v-if="group.latestPriced?.display_time" :datetime="group.latestPriced.display_time">{{ timeLabel(group.latestPriced.display_time) }}</time><span v-else>—</span><button type="button" class="records-download" title="下载Excel" aria-label="下载Excel" @click.stop="downloadSeller(group)"><Download aria-hidden="true" /></button></div></td>
          </tr></tbody>
        </table>
      </section>
    </main>

    <el-dialog v-model="dialogOpen" class="records-dialog" modal-class="records-overlay" :title="logTitle" width="min(1120px, calc(100vw - 32px))" align-center append-to-body destroy-on-close @closed="restoreFocus">
      <template #header="{ titleId, titleClass }">
        <div class="records-log-title">
          <h2 :id="titleId" :class="titleClass">{{ logTitle }}</h2>
          <div v-if="activeSeller" class="records-log-actions" role="group" aria-label="按北京时间的批次业务日期筛选和下载">
            <span class="records-log-date-label" title="按北京时间的批次业务日期筛选">业务日期</span>
            <div class="records-log-date-range">
              <el-date-picker type="date" aria-label="开始日期" :model-value="logRange.start"
                value-format="YYYY-MM-DD" popper-class="records-log-calendar" :clearable="false"
                :editable="false" :automatic-dropdown="false" :show-now="false"
                @update:model-value="changeLogDate('start', $event)" />
              <span aria-hidden="true">至</span>
              <el-date-picker type="date" aria-label="结束日期" :model-value="logRange.end"
                value-format="YYYY-MM-DD" popper-class="records-log-calendar" :clearable="false"
                :editable="false" :automatic-dropdown="false" :show-now="false"
                @update:model-value="changeLogDate('end', $event)" />
            </div>
            <button type="button" class="records-download records-log-download" title="下载所选日期的 Excel" aria-label="下载所选日期的 Excel"
              :disabled="!logItems.length" @click="downloadSeller({ name: activeSeller.name, items: logItems }, logRange)"><Download aria-hidden="true" /></button>
          </div>
        </div>
        <p v-if="downloadError" class="records-error" role="alert">{{ downloadError }}</p>
      </template>
      <SellerRecordLogs v-if="activeSeller" :key="activeSeller.id" :items="logItems"
        :empty-message="logRange.start === logRange.end ? '当天无记录。' : '所选日期范围内没有记录。'" />
    </el-dialog>
  </div>
</template>
