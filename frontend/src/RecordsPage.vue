<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { Download, Search } from '@element-plus/icons-vue'
import { timeLabel } from './api'
import { connectRecords } from './records-live'
import { groupBySeller, recordTone, countRecordStatuses, recordDisplayStates, recordDepartments, recordDateLabel, recordDateValue, type RecordData, type SellerGroup } from './records-model'
import { formatDecimal } from './decimal-display'
import { downloadSellerExcel } from './records-download'
import SellerRecordLogs from './SellerRecordLogs.vue'
import RecordsDots from './RecordsDots.vue'
import repricingLogo from './assets/repricing-logo.png'
import './records.css'

defineProps<{ mode: string }>()
const requestedDepartment = new URLSearchParams(window.location.search).get('department')
const department = ref(recordDepartments.find(entry => entry.id === requestedDepartment)?.id ?? recordDepartments[0]!.id)
const departments = recordDepartments
const data = ref<RecordData | null>(null)
const loading = ref(true)
const error = ref('')
const downloadError = ref('')
const dialogOpen = ref(false)
const logTitle = computed(() => `${activeSeller.value?.name ?? '销售员'}的提价日志`)
const selectedSeller = ref<string | null>(null)
const sellerQuery = ref('')
const searchInput = ref<HTMLInputElement | null>(null)
const groups = computed(() => groupBySeller(data.value?.items ?? []))
const counts = computed(() => countRecordStatuses(data.value?.items ?? []))
// 仅筛选当前部门已读取的姓名；保留完整人员记录，不改变部门汇总或下载内容。
const visibleGroups = computed(() => {
  const query = sellerQuery.value.trim().toLocaleLowerCase()
  return query ? groups.value.filter(group => group.name.toLocaleLowerCase().includes(query)) : groups.value
})
const activeSeller = computed(() => groups.value.find(group => group.id === selectedSeller.value))
// 标题只显示最近日期；完整日期性质与范围放在悬浮说明，避免页面出现“核验/更新”前缀。
const logDate = computed(() => recordDateValue(activeSeller.value?.items ?? []))
const logDateTitle = computed(() => recordDateLabel(activeSeller.value?.items ?? []))
const departmentName = computed(() => departments.find(entry => entry.id === department.value)?.name ?? '部门提价记录')
const sellerCount = computed(() => groups.value.filter(group => group.assigned).length)
const states = recordDisplayStates
let disconnect: (() => void) | undefined
let request = 0
let opener: HTMLElement | null = null

function stopLive() {
  ++request
  disconnect?.()
  disconnect = undefined
}

function startLive() {
  stopLive()
  if (document.visibilityState !== 'visible') return
  const current = request
  error.value = ''
  disconnect = connectRecords<RecordData>(department.value, response => {
    // 切部门或卸载后到达的旧连接消息不得覆盖新部门。
    if (current !== request) return
    data.value = response
    department.value = response.department ?? ''
    loading.value = false
    error.value = ''
    if (dialogOpen.value && !activeSeller.value) dialogOpen.value = false
  }, () => {
    if (current !== request) return
    loading.value = false
    error.value = '实时连接中断，正在重新连接；当前记录可能不是最新状态。'
  })
}

function onVisibilityChange() {
  if (document.visibilityState === 'visible') startLive()
  else stopLive()
}

function changeDepartment(id: string) {
  if (department.value === id) return
  department.value = id
  dialogOpen.value = false
  selectedSeller.value = null
  sellerQuery.value = ''
  data.value = null
  opener = null
  loading.value = true
  startLive()
}

function openSeller(group: SellerGroup, event: MouseEvent) {
  const target = event.currentTarget as HTMLElement
  opener = target instanceof HTMLButtonElement ? target : target.querySelector<HTMLButtonElement>('.records-person-button')
  opener?.focus()
  selectedSeller.value = group.id
  dialogOpen.value = true
}

async function downloadSeller(group: SellerGroup) {
  downloadError.value = ''
  try {
    await downloadSellerExcel(group, departmentName.value)
  } catch {
    downloadError.value = 'Excel 下载未成功，请重试。'
  }
}

function restoreFocus() {
  void nextTick(() => { if (opener?.isConnected) opener.focus() })
}

onMounted(() => {
  startLive()
  document.addEventListener('visibilitychange', onVisibilityChange)
})
onUnmounted(() => {
  stopLive()
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
        <div><strong>{{ departmentName }}</strong><span v-if="data">{{ sellerCount }} 位销售员，全部 {{ data.total }} 条记录<span v-if="groups.some(group => !group.assigned)">，含待分配记录</span></span></div>
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
        <div v-else-if="!data || !groups.length" class="records-empty motion-reveal"><h3>{{ error ? '记录暂未加载' : '暂无提价记录' }}</h3><p>{{ error ? '恢复连接后会自动重试。' : '部门产生提价处理记录后，会按销售员展示在这里。' }}</p></div>
        <div v-else-if="!visibleGroups.length" class="records-empty" role="status">
          <h3>未找到销售员</h3><p>请换一个姓名关键词。</p>
        </div>
        <table v-else class="records-table motion-reveal">
          <caption class="records-sr">{{ departmentName }}全部销售员提价记录，点击姓名查看本人详细日志</caption>
          <thead><tr><th scope="col">销售员 / 全部记录</th><th scope="col">最近商品 / SKU</th><th scope="col">店铺 / 站点</th><th scope="col" title="算价选中的订单修正一次毛利，按原币展示">修正一次毛利</th><th scope="col">最近营销价变动</th><th scope="col">上传价</th><th scope="col">处理状态</th><th scope="col">最近更新</th></tr></thead>
          <tbody><tr v-for="(group, index) in visibleGroups" :key="group.id" class="records-person-row" :data-seller="group.id" @click="openSeller(group, $event)">
            <td class="records-person-cell"><button class="records-person-button" :aria-label="`查看${group.name}的全部${group.items.length}条提价日志`" aria-haspopup="dialog" @click.stop="openSeller(group, $event)"><span class="records-avatar" :class="`tone-${index % 4}`" aria-hidden="true">{{ group.assigned ? group.name.slice(-1) : '?' }}</span><span><strong>{{ group.name }}</strong><small>共计{{ group.items.length }}条</small></span></button></td>
            <td class="records-product-cell"><strong>{{ group.latest.key.sku || 'SKU 未记录' }}</strong><small>商品 {{ group.latest.key.product_id || group.latest.key.item_id || '未记录' }}</small></td>
            <td class="records-shop-cell"><strong>{{ group.latest.shop_name || group.latest.key.shop_id || '店铺未记录' }}</strong><small>{{ group.latest.key.site }} / {{ group.latest.key.platform }}</small></td>
            <td class="records-margin-cell"><span class="records-mobile-label">修正一次毛利</span><strong>{{ formatDecimal(group.latest.first_margin?.amount ?? '未记录') }}</strong><small v-if="group.latest.first_margin">{{ group.latest.first_margin.currency }}</small></td>
            <td class="records-price-cell"><span class="records-mobile-label">最近营销价变动</span><div class="records-price-change"><span>{{ formatDecimal(group.latest.decision?.current_actual ?? '未记录') }}</span><i aria-hidden="true">→</i><strong>{{ formatDecimal(group.latest.execution?.actual_price ?? group.latest.decision?.target_actual ?? '未记录') }}</strong></div><small>{{ group.latest.execution?.actual_price == null ? '目标' : '回读' }} · {{ group.latest.decision?.trace.station_currency || '币种未记录' }}</small></td>
            <!-- 上传价取最近记录的持久提交意图，使用持久化十进制文本并限制展示为两位小数；不以建议价或回读价代替，也不表示已经生效。 -->
            <td class="records-upload-cell"><span class="records-mobile-label">上传价</span><strong>{{ formatDecimal(group.latest.execution?.uploaded_price ?? (group.latest.execution ? '未记录' : '未提交')) }}</strong><small v-if="group.latest.execution?.uploaded_price != null">{{ group.latest.decision?.trace.station_currency || '币种未记录' }}</small></td>
            <td class="records-progress-cell"><div class="records-status-list"><template v-for="state in states" :key="state"><span v-if="group.counts[state]" class="records-status" :class="recordTone(state)"><i></i>{{ state }} {{ group.counts[state] }}</span></template></div></td>
            <td class="records-time-cell"><div class="records-row-end"><time v-if="group.latest.display_time" :datetime="group.latest.display_time">{{ timeLabel(group.latest.display_time) }}</time><span v-else>—</span><button type="button" class="records-download" title="下载Excel" aria-label="下载Excel" @click.stop="downloadSeller(group)"><Download aria-hidden="true" /></button></div></td>
          </tr></tbody>
        </table>
      </section>
    </main>

    <el-dialog v-model="dialogOpen" class="records-dialog" modal-class="records-overlay" :title="logTitle" width="min(1120px, calc(100vw - 32px))" align-center append-to-body destroy-on-close @closed="restoreFocus">
      <template #header="{ titleId, titleClass }">
        <!-- 只显示一个最近日期，日期性质与完整范围放在悬浮说明，不挤占标题行。 -->
        <div class="records-log-title">
          <h2 :id="titleId" :class="titleClass">{{ logTitle }}</h2>
          <span class="records-log-date" :title="logDateTitle">{{ logDate }}</span>
          <button v-if="activeSeller" type="button" class="records-download records-log-download" title="下载Excel" aria-label="下载Excel" @click.stop="downloadSeller(activeSeller)"><Download aria-hidden="true" /></button>
        </div>
        <p v-if="downloadError" class="records-error" role="alert">{{ downloadError }}</p>
      </template>
      <SellerRecordLogs v-if="activeSeller" :seller="activeSeller" />
    </el-dialog>
  </div>
</template>
