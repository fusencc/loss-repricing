<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { api, errorMessage, timeLabel, type ItemStatus } from './api'
import { groupBySeller, recordTone, recordStatusLabel, recordDepartments, type RecordData, type SellerGroup } from './records-model'
import SellerRecordLogs from './SellerRecordLogs.vue'
import './records.css'

defineProps<{ mode: string }>()
const requestedDepartment = new URLSearchParams(window.location.search).get('department')
const department = ref(recordDepartments.find(entry => entry.id === requestedDepartment)?.id ?? recordDepartments[0]!.id)
const departments = recordDepartments
const data = ref<RecordData | null>(null)
const loading = ref(true)
const refreshing = ref(false)
const error = ref('')
const dialogOpen = ref(false)
const selectedSeller = ref<string | null>(null)
const groups = computed(() => groupBySeller(data.value?.items ?? []))
const activeSeller = computed(() => groups.value.find(group => group.id === selectedSeller.value))
const departmentName = computed(() => departments.find(entry => entry.id === department.value)?.name ?? '部门提价记录')
const sellerCount = computed(() => groups.value.filter(group => group.assigned).length)
const states: ItemStatus[] = ['已核验', '待核验', '待处理', '本轮不调']
let controller: AbortController | undefined
let request = 0
let timer: number | undefined
let opener: HTMLElement | null = null

async function load(quiet = false) {
  const current = ++request
  controller?.abort()
  controller = new AbortController()
  if (!quiet) loading.value = true
  refreshing.value = true
  error.value = ''
  try {
    const response = await api.get<RecordData>('/records', { signal: controller.signal,
      params: department.value ? { department: department.value } : {} })
    if (current !== request) return
    data.value = response.data
    department.value = response.data.department ?? ''
    if (dialogOpen.value && !activeSeller.value) dialogOpen.value = false
  } catch (cause) {
    if (current === request) error.value = errorMessage(cause)
  } finally {
    if (current === request) { loading.value = false; refreshing.value = false }
  }
}

function changeDepartment(id: string) {
  if (department.value === id) return
  department.value = id
  dialogOpen.value = false
  selectedSeller.value = null
  data.value = null
  opener = null
  void load()
}

function openSeller(group: SellerGroup, event: MouseEvent) {
  const target = event.currentTarget as HTMLElement
  opener = target instanceof HTMLButtonElement ? target : target.querySelector<HTMLButtonElement>('.records-person-button')
  opener?.focus()
  selectedSeller.value = group.id
  dialogOpen.value = true
}

function restoreFocus() {
  void nextTick(() => { if (opener?.isConnected) opener.focus() })
}

onMounted(() => {
  void load()
  timer = window.setInterval(() => {
    if (document.visibilityState === 'visible' && !refreshing.value) void load(true)
  }, 60000)
})
onUnmounted(() => { ++request; controller?.abort(); window.clearInterval(timer) })
</script>

<template>
  <div class="records-page">
    <div class="records-aura" aria-hidden="true"></div>
    <main class="records-shell">
      <header class="records-header">
        <div>
          <div class="records-title"><span class="records-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M4 18h16M5 14l5-5 4 3 5-7M14 5h5v5" stroke-linecap="round" stroke-linejoin="round"/></svg></span><h1>提价日志</h1></div>
        </div>
        <div class="records-header-end">
          <nav class="records-departments" aria-label="切换部门"><button v-for="entry in departments" :key="entry.id" type="button" :aria-pressed="department === entry.id" :class="{ active: department === entry.id }" aria-controls="records-list" @click="changeDepartment(entry.id)">{{ entry.name }}</button></nav>
        </div>
      </header>

      <div class="records-overview" aria-live="polite">
        <div><strong>{{ departmentName }}</strong><span v-if="data">{{ sellerCount }} 位销售员，全部 {{ data.total }} 条记录<span v-if="groups.some(group => !group.assigned)">，含待分配记录</span></span></div>
        <div v-if="data" class="records-counts"><span v-for="state in states.filter(state => state !== '本轮不调')" :key="state"><i :class="recordTone(state)"></i>{{ recordStatusLabel(state) }} <b>{{ data.counts[state] ?? 0 }}</b></span></div>
      </div>
      <p v-if="error" class="records-error" role="alert"><strong>{{ data ? '更新未成功，以下保留最近读取的数据。' : '暂时无法读取记录。' }}</strong>{{ error }}。可切换部门或刷新页面，系统也会自动重试。</p>

      <section id="records-list" class="records-glass" :aria-busy="loading">
        <div class="records-panel-heading"><h2>销售员提价记录</h2></div>
        <div v-if="loading" class="records-empty" role="status"><h3>正在读取部门日志</h3><p>记录读取后会在这里完整展示。</p></div>
        <div v-else-if="!data || !groups.length" class="records-empty"><h3>{{ error ? '记录暂未加载' : '暂无提价记录' }}</h3><p>{{ error ? '恢复连接后会自动重试。' : '部门产生提价处理记录后，会按销售员展示在这里。' }}</p></div>
        <table v-else class="records-table">
          <caption class="records-sr">{{ departmentName }}全部销售员提价记录，点击姓名查看本人详细日志</caption>
          <thead><tr><th scope="col">销售员 / 全部记录</th><th scope="col">最近商品 / SKU</th><th scope="col">店铺 / 站点</th><th scope="col">最近原实际价 → 目标价</th><th scope="col">全部处理进度</th><th scope="col">最近更新</th></tr></thead>
          <tbody><tr v-for="(group, index) in groups" :key="group.id" class="records-person-row" :data-seller="group.id" @click="openSeller(group, $event)">
            <td class="records-person-cell"><button class="records-person-button" :aria-label="`查看${group.name}的全部${group.items.length}条提价日志`" aria-haspopup="dialog" @click.stop="openSeller(group, $event)"><span class="records-avatar" :class="`tone-${index % 4}`" aria-hidden="true">{{ group.assigned ? group.name.slice(-1) : '?' }}</span><span><strong>{{ group.name }}</strong><small>共计{{ group.items.length }}条</small></span></button></td>
            <td class="records-product-cell"><strong>{{ group.latest.key.sku || 'SKU 未记录' }}</strong><small>商品 {{ group.latest.key.product_id || group.latest.key.item_id || '未记录' }}</small></td>
            <td class="records-shop-cell"><strong>{{ group.latest.shop_name || group.latest.key.shop_id || '店铺未记录' }}</strong><small>{{ group.latest.key.site }} / {{ group.latest.key.platform }}</small></td>
            <td class="records-price-cell"><span class="records-mobile-label">最近原实际价 → 目标价</span><div class="records-price-change"><span>{{ group.latest.decision?.current_actual ?? '未记录' }}</span><i aria-hidden="true">→</i><strong>{{ group.latest.decision?.target_actual ?? '未记录' }}</strong></div><small>{{ group.latest.decision?.trace.station_currency || '币种未记录' }}</small></td>
            <td class="records-progress-cell"><div class="records-status-list"><template v-for="state in states" :key="state"><span v-if="group.counts[state]" class="records-status" :class="recordTone(state)"><i></i>{{ recordStatusLabel(state) }} {{ group.counts[state] }}</span></template></div></td>
            <td class="records-time-cell"><time :datetime="group.latest.updated_at">{{ timeLabel(group.latest.updated_at) }}</time></td>
          </tr></tbody>
        </table>
      </section>
    </main>

    <el-dialog v-model="dialogOpen" class="records-dialog" modal-class="records-overlay" :title="`${activeSeller?.name ?? '销售员'}的提价日志`" width="min(1120px, calc(100vw - 32px))" align-center append-to-body destroy-on-close @closed="restoreFocus">
      <SellerRecordLogs v-if="activeSeller" :seller="activeSeller" />
    </el-dialog>
  </div>
</template>
