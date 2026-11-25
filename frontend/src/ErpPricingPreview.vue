<script setup lang="ts">
import { computed, onMounted, ref, shallowRef } from 'vue'
import { isAxiosError } from 'axios'
import { ElPagination } from 'element-plus'
import { api, display, errorMessage, timeLabel, type ErpReconciliation, type Identifier, type Item } from './api'

const props = defineProps<{
  fileMode: boolean
  jobBusy: boolean
  openReport: Pick<ErpReconciliation, 'capture_id' | 'preview_id'> | null
}>()
const emit = defineEmits<{
  reconcile: [captureId: string, previewId: string]
  openBatch: [batchId: Identifier]
}>()

type Target = Item['key']
type PreviewDecision = NonNullable<Item['decision']> & {
  key: Target; reason: string; status: Item['status']
  trace: { station_currency?: string }
}
interface Issue {
  order_id: string; detail_id?: string; sources: string[]; code: string; reason: string; target?: Target
}
interface ExportMapping {
  summary: {
    detail_rows: number; mapped_rows: number; unresolved_rows: number; unique_targets: number
    cross_order_targets: number; sku_loss_present_rows: number
  }
  rows: {
    order_id: string | null; row_number: number; erp_sku: string | null; quantity: string | null; reason: string
    target: { shop: string; site: string; product_id: string; item_id: string; platform_sku: string } | null
  }[]
  evidence_conflicts: { order_id: string; detail_ref: string | null; reason: string }[]
}
interface PreviewReport {
  preview_id: string; capture_id: string; business_date: string; captured_at: string
  latest_capture_observation_at: string; evaluated_at: string | null; synthetic: boolean
  batch_input_available: boolean
  reconciliation_batch_id?: Identifier
  source_coverage: {
    source: string; candidate_orders: number | null; finance_rows: number | null
    finance_complete: boolean; preparation_complete: boolean
  }[]
  summary: {
    source_rows: number; unique_orders: number; normalized_detail_lines: number
    preparation_issues: number; decisions: number; ready_prices: number
  }
  global_issues: string[]
  export_mapping?: ExportMapping
  issues: Issue[]
  decisions: PreviewDecision[]
  references: { lines: Record<string, { order_id: string; detail_id: string }> }
}

const sourceNames: Record<string, string> = {
  loss_allocatable: '亏损 · 可配货',
  loss_pending: '亏损 · 待处理',
  blocked_allocatable: '禁止发货 · 可配货',
  blocked_pending: '禁止发货 · 待处理',
}
const captureId = ref(props.openReport?.capture_id ?? '')
const previewId = ref(props.openReport?.preview_id ?? '')
const file = shallowRef<File | null>(null)
const erpDetails = shallowRef<File | null>(null)
const salesVariants = shallowRef<File | null>(null)
const incompleteExports = computed(() => Boolean(erpDetails.value) !== Boolean(salesVariants.value))
const busy = ref(false)
const failure = ref('')
const report = shallowRef<PreviewReport | null>(null)
const pageSize = 25
const issuePage = ref(1)
const decisionPage = ref(1)
const mappingPage = ref(1)

function pageRows<T>(rows: T[], page: number) {
  return rows.slice((page - 1) * pageSize, page * pageSize)
}
const visibleIssues = computed(() => pageRows(report.value?.issues ?? [], issuePage.value))
const visibleDecisions = computed(() => pageRows(report.value?.decisions ?? [], decisionPage.value))
const visibleMappings = computed(() => pageRows(report.value?.export_mapping?.rows ?? [], mappingPage.value))

function selectedFile(event: Event) {
  return (event.target as HTMLInputElement).files?.[0] ?? null
}

function selectEvidence(event: Event) {
  file.value = selectedFile(event)
}

async function run(reopen: boolean) {
  if (busy.value) return
  const capture = captureId.value.trim()
  const preview = previewId.value.trim()
  if (!capture || (reopen ? !preview : incompleteExports.value)) return
  busy.value = true
  failure.value = ''
  report.value = null
  issuePage.value = 1
  decisionPage.value = 1
  mappingPage.value = 1
  const path = '/sources/erp/captures/' + encodeURIComponent(capture) + '/pricing-previews'
  try {
    if (reopen) {
      report.value = (await api.get<PreviewReport>(path + '/' + encodeURIComponent(preview))).data
    } else {
      let evidence: object | null = null
      if (file.value) {
        try {
          const value: unknown = JSON.parse(await file.value.text())
          if (value == null || typeof value !== 'object' || Array.isArray(value)) throw new Error()
          evidence = value
        } catch {
          throw new Error('补充证据须为 UTF-8 编码的 JSON 对象')
        }
      }
      if (erpDetails.value && salesVariants.value) {
        const files = new FormData()
        files.append('erp_details', erpDetails.value)
        files.append('sales_variants', salesVariants.value)
        if (evidence && file.value) files.append('evidence_json', file.value)
        report.value = (await api.post<PreviewReport>(path + '/from-exports', files, { timeout: 60000 })).data
      } else {
        report.value = (await api.post<PreviewReport>(path, evidence)).data
      }
      previewId.value = report.value.preview_id
    }
  } catch (error) {
    const path = isAxiosError(error) && Array.isArray(error.response?.data?.detail)
      ? error.response.data.detail[0]?.loc
      : null
    const field = Array.isArray(path) && path[0] === 'body'
      && path.slice(1).every(part => typeof part === 'number' || /^[a-z_]{1,32}$/.test(String(part)))
      ? path.slice(1).join('.')
      : ''
    failure.value = errorMessage(error) + (field ? ' · 证据位置 ' + field : '')
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  if (props.openReport) void run(true)
})

function amount(value: string | null, currency?: string) {
  return value == null ? '—' : [display(value), currency].filter(Boolean).join(' ')
}

function selectedLine(rowId: string | null) {
  const line = rowId ? report.value?.references.lines[rowId] : undefined
  return line ? line.order_id + ' / ' + line.detail_id : '—'
}

function download() {
  if (!report.value) return
  const url = URL.createObjectURL(new Blob([JSON.stringify(report.value, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = 'erp-pricing-preview-' + report.value.preview_id + '.json'
  link.click()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <section class="panel settings-panel pricing-preview">
    <header class="panel-heading"><h2>ERP 采集对账与只读算价</h2></header>
    <p class="muted">输入已保存的四类批量采集编号。两份原始导出可核对在线变体，补充证据用于只读算价。</p>
    <div class="preview-inputs">
      <label for="pricing-capture-id">采集编号</label>
      <el-input id="pricing-capture-id" v-model="captureId" :disabled="busy" placeholder="erp-capture-…" />
      <label for="pricing-erp-details">ERP 商品明细 XLSX</label>
      <input id="pricing-erp-details" type="file" accept=".xlsx" :disabled="busy" @change="erpDetails = selectedFile($event)" />
      <label for="pricing-sales-variants">销售在线变体 XLSX</label>
      <input id="pricing-sales-variants" type="file" accept=".xlsx" :disabled="busy" @change="salesVariants = selectedFile($event)" />
      <label for="pricing-evidence">补充证据 JSON（可选）</label>
      <input id="pricing-evidence" type="file" accept=".json,application/json" :disabled="busy" @change="selectEvidence" />
      <label for="pricing-preview-id">报告编号（回读时填写）</label>
      <el-input id="pricing-preview-id" v-model="previewId" :disabled="busy" />
    </div>
    <p v-if="incompleteExports" class="muted">选择两份原始导出后即可核对在线变体。</p>
    <div class="form-actions">
      <el-button type="primary" :disabled="busy || !captureId.trim() || incompleteExports" @click="run(false)">生成只读报告</el-button>
      <el-button :disabled="busy || !captureId.trim() || !previewId.trim()" @click="run(true)">打开已保存报告</el-button>
      <el-button v-if="report" @click="download">下载算价对账报告</el-button>
      <el-button v-if="report?.reconciliation_batch_id != null"
        @click="emit('openBatch', report.reconciliation_batch_id)">打开文件对账批次</el-button>
      <el-button v-else-if="fileMode && report?.batch_input_available" :disabled="busy || jobBusy"
        @click="emit('reconcile', report.capture_id, report.preview_id)">生成文件对账批次</el-button>
    </div>
    <p v-if="busy" role="status">正在读取已保存的采集记录和补充证据。</p>
    <el-alert v-if="failure" :title="failure" type="error" :closable="false" show-icon />
    <template v-if="report">
      <p v-if="fileMode && !report.batch_input_available" class="muted">有规范快照的报告可生成文件对账批次。请使用已核对的证据重新生成报告。</p>
      <p class="preview-meta">
        {{ report.synthetic ? '合成样例 · 只读回放' : '已保存 ERP 采集 · 只读回放' }}<br />
        采集编号 <code>{{ report.capture_id }}</code><br />
        报告编号 <code>{{ report.preview_id }}</code><br />
        采集开始 {{ timeLabel(report.captured_at) }} · 最后导出观察 {{ timeLabel(report.latest_capture_observation_at) }}<br />
        回放时点 {{ timeLabel(report.evaluated_at) }}（北京时间）
      </p>
      <p>
        来源财务行 {{ report.summary.source_rows }} 条，唯一订单 {{ report.summary.unique_orders }} 笔，
        可交给引擎的明细 {{ report.summary.normalized_detail_lines }} 条，
        判定 {{ report.summary.decisions }} 项，算价通过 {{ report.summary.ready_prices }} 项。
      </p>
      <ul v-if="report.global_issues.length" class="missing-list">
        <li v-for="issue in report.global_issues" :key="issue">{{ issue }}</li>
      </ul>
      <el-table :data="report.source_coverage" size="small">
        <el-table-column label="来源" min-width="146">
          <template #default="{ row }">{{ sourceNames[row.source] }}</template>
        </el-table-column>
        <el-table-column prop="candidate_orders" label="候选订单" min-width="100" />
        <el-table-column prop="finance_rows" label="财务行" min-width="95" />
        <el-table-column label="名单与财务对账" min-width="130">
          <template #default="{ row }">{{ row.finance_complete ? '已核对' : '待核对' }}</template>
        </el-table-column>
        <el-table-column label="可定位明细覆盖" min-width="130">
          <template #default="{ row }">{{ row.preparation_complete ? '已定位' : '有未定位缺项' }}</template>
        </el-table-column>
      </el-table>
      <template v-if="report.export_mapping">
        <h3>原始导出映射</h3>
        <p>
          ERP 明细 {{ report.export_mapping.summary.detail_rows }} 行，
          匹配 {{ report.export_mapping.summary.mapped_rows }} 行，
          待核对 {{ report.export_mapping.summary.unresolved_rows }} 行，
          在线变体 {{ report.export_mapping.summary.unique_targets }} 个，
          跨订单重复变体 {{ report.export_mapping.summary.cross_order_targets }} 个。
        </p>
        <p class="muted">
          SKU 修正毛利有值 {{ report.export_mapping.summary.sku_loss_present_rows }} 行。
          完整行引用可从报告下载，算价须补齐稳定明细 ID 和亏损证据。
        </p>
        <el-alert v-if="report.export_mapping.evidence_conflicts.length"
          :title="'补充证据和原始导出有 ' + report.export_mapping.evidence_conflicts.length + ' 处不一致，逐条原因见下载报告'"
          type="warning" :closable="false" />
        <el-table :data="visibleMappings" size="small" empty-text="原始导出无明细行">
          <el-table-column prop="order_id" label="订单号" min-width="155" />
          <el-table-column prop="row_number" label="Excel 行" min-width="80" />
          <el-table-column prop="erp_sku" label="ERP 本地 SKU" min-width="120" />
          <el-table-column label="数量" min-width="75">
            <template #default="{ row }">{{ display(row.quantity) }}</template>
          </el-table-column>
          <el-table-column label="销售在线变体" min-width="218">
            <template #default="{ row }">
              <template v-if="row.target">
                {{ row.target.site }} · 商品 {{ row.target.item_id }}<br />
                <span class="muted">{{ row.target.platform_sku }} · 产品 {{ row.target.product_id }}</span>
              </template>
              <span v-else>—</span>
            </template>
          </el-table-column>
          <el-table-column prop="reason" label="映射状态" min-width="220" />
        </el-table>
        <el-pagination v-if="report.export_mapping.rows.length > pageSize" v-model:current-page="mappingPage"
          small layout="prev, pager, next" :page-size="pageSize" :total="report.export_mapping.rows.length" />
      </template>
      <template v-if="report.decisions.length">
        <h3>补亏判定</h3>
        <el-table :data="visibleDecisions" size="small">
          <el-table-column label="在线商品 / 子 SKU" min-width="160">
            <template #default="{ row }">{{ row.key.site }} · {{ row.key.item_id }} / {{ row.key.sku }}<br />
              <span class="muted">{{ row.key.shop_id }} · {{ row.key.warehouse }}</span>
            </template>
          </el-table-column>
          <el-table-column label="选中订单 / 明细" min-width="145">
            <template #default="{ row }">{{ selectedLine(row.selected_row_id) }}</template>
          </el-table-column>
          <el-table-column label="单件亏损" min-width="100">
            <template #default="{ row }">{{ amount(row.unit_loss, row.loss_currency) }}</template>
          </el-table-column>
          <el-table-column label="当前实际价" min-width="110">
            <template #default="{ row }">{{ amount(row.current_actual, row.trace.station_currency) }}</template>
          </el-table-column>
          <el-table-column label="目标实际价" min-width="110">
            <template #default="{ row }">{{ amount(row.target_actual, row.trace.station_currency) }}</template>
          </el-table-column>
          <el-table-column label="含税上传建议" min-width="115">
            <template #default="{ row }">{{ amount(row.upload_price, row.trace.station_currency) }}</template>
          </el-table-column>
          <el-table-column label="结果与原因" min-width="210">
            <template #default="{ row }">{{ row.ready ? '算价通过' : row.status }} · {{ row.reason }}</template>
          </el-table-column>
        </el-table>
        <ElPagination v-if="report.decisions.length > pageSize" v-model:current-page="decisionPage"
          small layout="prev, pager, next" :page-size="pageSize" :total="report.decisions.length" />
      </template>
      <template v-if="report.issues.length">
        <h3>数据准备缺项（{{ report.summary.preparation_issues }}）</h3>
        <el-table :data="visibleIssues" size="small">
          <el-table-column prop="order_id" label="订单号" min-width="165" />
          <el-table-column prop="detail_id" label="明细 ID" min-width="100" />
          <el-table-column label="来源" min-width="185">
            <template #default="{ row }">{{ row.sources.map((name: string) => sourceNames[name]).join('、') }}</template>
          </el-table-column>
          <el-table-column label="在线子 SKU" min-width="125">
            <template #default="{ row }">{{ row.target?.sku ?? '待映射' }}</template>
          </el-table-column>
          <el-table-column prop="reason" label="原因" min-width="210" />
        </el-table>
        <ElPagination v-if="report.issues.length > pageSize" v-model:current-page="issuePage"
          small layout="prev, pager, next" :page-size="pageSize" :total="report.issues.length" />
      </template>
    </template>
  </section>
</template>

<style scoped>
.preview-inputs {
  display: grid;
  grid-template-columns: max-content minmax(0, 32rem);
  gap: 0.55rem 0.8rem;
  align-items: center;
  margin-top: 1rem;
}
.preview-inputs input { max-width: 100%; }
.preview-meta { overflow-wrap: anywhere; }
.pricing-preview h3 { margin: 1rem 0 0.4rem; }
@media (max-width: 560px) {
  .preview-inputs { grid-template-columns: minmax(0, 1fr); }
}
</style>
