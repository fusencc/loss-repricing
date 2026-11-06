<script setup lang="ts">
import { ref } from 'vue'
import { api, errorMessage, timeLabel } from './api'

interface SourceGroup {
  source: string; label: string; endpoint: string; requested_scope: Record<string, unknown>
  read_succeeded: boolean; sampled_rows: number; reported_total: number | null
  coverage: string; observed_shop_ids: string[]; observed_order_ids: string[]; missing_order_ids: number
  error: string | null; error_kind: string | null; http_status: number | null; business_code: number | null
}
interface ValidationReport {
  report_version: string; source_version: string; queried_at: string; message: string
  summary: { list_reads_succeeded: number; list_reads_failed: number; sampled_rows: number
    detail_reads_succeeded: number; detail_reads_failed: number; detail_rows: number; detail_issue_count: number }
  groups: SourceGroup[]
  detail_issues: { order_id: string | null; row_number: number | null; fields: string[]; reason: string
    http_status?: number | null; business_code?: number | null }[]
  pending_confirmations: string[]; next_steps: string[]
}
const busy = ref(false)
const limit = ref(2)
const failure = ref('')
const report = ref<ValidationReport | null>(null)
const coverageLabels: Record<string, string> = {
  read_failed: '读取失败', total_unknown: '总数未知', count_inconsistent: '总数与返回条数矛盾',
  first_page_only: '仅第一页，仍有未读取记录', reported_empty: '接口自报零条，待人工对照',
  reported_total_observed: '已观察接口自报条数，尚未证明权限完整',
}
function errorLabel(group: SourceGroup) {
  if (group.error_kind === 'access_denied') return 'HTTP 403：该请求被拒绝'
  if (group.error_kind === 'authentication_rejected') return 'HTTP 401：认证被拒绝'
  if (group.error_kind === 'business_failure') return `业务失败（${group.business_code}），权限原因尚未确认`
  return group.error || '读取成功不代表范围完整'
}
async function query() {
  if (busy.value) return
  busy.value = true
  failure.value = ''
  report.value = null
  try {
    // 四组及逐订单明细为串行只读请求，超时不自动重试，避免重复取数。
    report.value = (await api.get<ValidationReport>('/sources/erp/validation', {
      params: { limit: limit.value }, timeout: 1800000,
    })).data
  } catch (error) { failure.value = errorMessage(error) }
  finally { busy.value = false }
}
function download() {
  if (!report.value) return
  const url = URL.createObjectURL(new Blob([JSON.stringify(report.value, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `erp-validation-${report.value.queried_at.replace(/[^0-9]/g, '')}.json`
  link.click()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <section class="panel settings-panel">
    <header class="panel-heading"><h2>ERP 数据获取验证</h2></header>
    <p class="muted">主动访问真实 ERP，读取四组候选的第一页并补订单明细。只读验证独立于演示模式，不计算价格、不提交调价、不修改试点。</p>
    <div class="form-actions">
      <label for="erp-sample-limit">每组抽样条数</label>
      <el-input-number id="erp-sample-limit" v-model="limit" :min="1" :max="10" :value-on-clear="2" :disabled="busy" aria-label="每组抽样条数" />
      <el-button type="primary" :loading="busy" @click="query">运行真实只读验证</el-button>
      <el-button v-if="report" @click="download">下载诊断报告</el-button>
    </div>
    <p v-if="busy" role="status">正在读取四组列表及订单明细，可能需要数分钟；不会自动重试。</p>
    <el-alert v-if="failure" :title="failure" type="error" :closable="false" show-icon />
    <template v-if="report">
      <el-alert :title="report.message" type="warning" :closable="false" show-icon />
      <p>查询时间：{{ timeLabel(report.queried_at) }} · 网页登录成功 · 账号范围未验收 · 尚不能算价</p>
      <p>列表读取成功 {{ report.summary.list_reads_succeeded }} 组，失败 {{ report.summary.list_reads_failed }} 组；
        样本 {{ report.summary.sampled_rows }} 条；明细读取成功 {{ report.summary.detail_reads_succeeded }} 单，失败 {{ report.summary.detail_reads_failed }} 单；
        明细字段及读取问题 {{ report.summary.detail_issue_count }} 项。</p>
      <el-table :data="report.groups" empty-text="尚无读取证据">
        <el-table-column prop="label" label="候选来源" min-width="170" />
        <el-table-column label="读取结果" min-width="110"><template #default="{ row }">{{ row.read_succeeded ? '读取成功' : '读取失败' }}</template></el-table-column>
        <el-table-column prop="sampled_rows" label="样本条数" min-width="90" />
        <el-table-column label="接口自报总数" min-width="115"><template #default="{ row }">{{ row.reported_total ?? '未知' }}</template></el-table-column>
        <el-table-column label="覆盖情况" min-width="260"><template #default="{ row }">{{ coverageLabels[row.coverage] || row.coverage }}</template></el-table-column>
        <el-table-column label="请求与权限证据" min-width="250"><template #default="{ row }">{{ errorLabel(row) }}</template></el-table-column>
      </el-table>
      <details v-for="group in report.groups" :key="group.source" class="evidence">
        <summary>{{ group.label }}：筛选条件与实际观察范围</summary>
        <p>接口：{{ group.endpoint }}；缺少订单标识 {{ group.missing_order_ids }} 条。</p>
        <pre>{{ JSON.stringify(group.requested_scope, null, 2) }}</pre>
        <p>样本订单标识（去重）：{{ group.observed_order_ids.join('、') || '无可用订单标识' }}，可与相同筛选的人工导出逐项核对。</p>
        <p>样本出现的店铺 ID：{{ group.observed_shop_ids.join('、') || '无可用店铺标识' }}。未出现不能判定无权限，出现也不代表该店铺全部数据可读。</p>
      </details>
      <details v-if="report.detail_issues.length" class="evidence">
        <summary>展开明细读取及字段问题（{{ report.detail_issues.length }} 项）</summary>
        <el-table :data="report.detail_issues" max-height="360">
          <el-table-column prop="order_id" label="订单标识" min-width="180" />
          <el-table-column prop="row_number" label="明细行号" min-width="95" />
          <el-table-column label="原始字段" min-width="150"><template #default="{ row }">{{ row.fields.join('、') || '—' }}</template></el-table-column>
          <el-table-column prop="reason" label="问题说明" min-width="260" />
          <el-table-column label="接口状态" min-width="180"><template #default="{ row }">{{ row.http_status != null ? `HTTP ${row.http_status}` : row.business_code != null ? `业务码 ${row.business_code}（原因待核实）` : '无明确状态码' }}</template></el-table-column>
        </el-table>
      </details>
      <h3>仍需确认的数据与公式</h3>
      <ul class="missing-list"><li v-for="entry in report.pending_confirmations" :key="entry">{{ entry }}</li></ul>
      <h3>下一步对账</h3>
      <ol class="missing-list"><li v-for="entry in report.next_steps" :key="entry">{{ entry }}</li></ol>
      <p class="muted">报告版本 {{ report.report_version }} · 来源版本 {{ report.source_version }}。下载内容包含筛选范围和业务标识，请在受控位置保存，不要提交 Git。</p>
    </template>
  </section>
</template>
