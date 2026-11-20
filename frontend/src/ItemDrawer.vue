<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import ItemCoordination from './ItemCoordination.vue'
import SkuPriceReferenceView from './SkuPriceReference.vue'
import { display, statusType, stateLabel, timeLabel, type Item, type SkuPriceReference, type PriceLimit } from './api'

const props = defineProps<{ actorRole: string; actorId: string; admin: boolean; open: boolean; item: Item | null; loading: boolean; error: string; busy: boolean }>()
const emit = defineEmits<{ close: []; refresh: []; reevaluate: [id: Item['id'], note: string] }>()
const note = ref('')
watch(() => props.item?.id, () => { note.value = '' })

const identity = {
  platform: '平台', shop_id: '店铺 ID', site: '站点', product_id: '产品 ID',
  item_id: '商品 ID', sku: '子 SKU', warehouse: '仓配',
} as const

const decisionFields = {
  current_actual: '当前实际价', discount: '折扣系数',
  unit_loss: '单件亏损', loss_currency: '亏损原币',
  station_loss: '单件亏损（站点币）', target_actual: '目标实际价（站点币）',
  upload_price: '含 VAT 上传价', predicted_profit: '预测利润额（见公式版本）',
  selected_row_id: '选中来源行', source_row_ids: '候选来源行',
} as const

const traceLabels: Record<string, string> = {
  warning_codes: '告警类型', business_date: '业务日期', evaluated_at: '评估时间', as_of: '快照采集时间',
  fx_rates: '本日汇率（每 1 CNY）', complete_sources: '已齐备的候选来源',
  source_versions: '来源版本', source_rows: '原始候选行', online_rows: '在线快照',
  warnings: '计算告警', source_issues: '来源异常记录', online_profit: '在线利润校验依据',
  site_contract: '站点接口合同', sales_window: '仓库七日参考销量',
  sku_price_reference: '同 SKU Top3 营销参考价',
  price_limit: '参考价上限与压减',
  excluded_source_rows: '排除的候选行', candidate_unit_losses: '候选单件亏损（原币）',
  station_currency: '站点币', loss_fx_rate: '原币汇率（每 1 CNY）', station_fx_rate: '站点币汇率（每 1 CNY）',
  raw_upload_price: '舍入前含 VAT 上传价', rounded_actual: '舍入后实际价',
  actual_price_error: '实际价误差', allowed_error: '允许误差', target_profit: '目标价利润额（按公式版本）',
  quantity: '数量', loss_grain: '亏损粒度', fx_rate: '换算汇率', fx_date: '汇率日期',
  rate: '换算汇率', station_loss: '站点币亏损', current_actual: '当前实际价',
  discount: '折扣系数', revenue_factor: '费用模型收入系数', fixed_cost: '费用模型固定成本',
  target_actual: '目标实际价', upload_price: '含 VAT 上传价', predicted_profit: '预测利润额（见公式版本）',
  retail: '含 VAT 零售价', marketing_price: '营销价', source: '来源',
}
// 仅展示后端保存的实际价校正额；历史缺项显示未记录，不从旧证据重算。
const onlineProfitCorrection = computed(() => {
  const trace = props.item?.decision?.trace
  if (!trace || typeof trace !== 'object' || Array.isArray(trace)) return { amount: null, currency: '' }
  const fields = trace as Record<string, unknown>
  const evidence = fields.online_profit
  const amount = evidence && typeof evidence === 'object' && !Array.isArray(evidence)
    ? (evidence as Record<string, unknown>).correction_amount : undefined
  return { amount: typeof amount === 'string' ? amount : null,
    currency: typeof fields.station_currency === 'string' ? fields.station_currency : '' }
})
const calculationWarnings = computed(() => {
  const trace = props.item?.decision?.trace
  if (!trace || typeof trace !== 'object' || Array.isArray(trace)) return []
  const warnings = (trace as Record<string, unknown>).warnings
  return Array.isArray(warnings) ? warnings.filter((value): value is string => typeof value === 'string') : []
})
const traceRows = computed(() => {
  const trace = props.item?.decision?.trace
  return trace && typeof trace === 'object' && !Array.isArray(trace) ? Object.entries(trace) : []
})
const skuPriceReference = computed(() => {
  const trace = props.item?.decision?.trace
  return trace && typeof trace === 'object' && !Array.isArray(trace)
    ? (trace as { sku_price_reference?: SkuPriceReference }).sku_price_reference : null
})
const priceLimit = computed(() => {
  const trace = props.item?.decision?.trace
  return trace && typeof trace === 'object' && !Array.isArray(trace)
    ? (trace as { price_limit?: PriceLimit }).price_limit : null
})
</script>

<template>
  <el-drawer :model-value="open" size="min(690px, 100vw)" title="商品与计算详情" @close="emit('close')">
    <div v-loading="loading" class="drawer-body">
      <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
      <template v-if="item">
        <div class="drawer-heading">
          <div>
            <p class="eyebrow">记录 #{{ item.id }} · 批次 #{{ item.batch_id }}</p>
            <h2>{{ item.key.sku }}</h2>
            <p class="muted">{{ item.reason || '—' }}</p>
          </div>
          <el-tag :type="statusType(item.status)" effect="light">{{ item.status }}</el-tag>
        </div>
        <dl class="detail-grid">
          <div v-for="(label, key) in identity" :key="key"><dt>{{ label }}</dt><dd>{{ display(item.key[key]) }}</dd></div>
          <div><dt>更新时间（北京时间）</dt><dd>{{ timeLabel(item.updated_at) }}</dd></div>
        </dl>
        <ItemCoordination v-if="!item.read_only" :actor-role="actorRole" :actor-id="actorId" :item="item" @refresh="emit('refresh')" />
        <section v-if="item.decision" class="drawer-section">
          <h3>补亏计算</h3>
          <el-alert v-for="warning in calculationWarnings" :key="warning" :title="warning" type="warning" show-icon :closable="false" />
          <p class="muted">先计算补亏及 1% 在线利润率所需价格；有参考价时优先参考上限，利润不足或需要降价时也按参考上限调整，再按折扣反推上传价。没有参考价时沿用原规则。</p>
          <dl class="detail-grid">
            <div v-for="(label, key) in decisionFields" :key="key">
              <dt>{{ label }}</dt>
              <dd>{{ key === 'source_row_ids' ? item.decision.source_row_ids?.join('、') || '—' : display(item.decision[key]) }}</dd>
            </div>
            <div><dt>在线利润校正额（站点币）</dt><dd>{{ onlineProfitCorrection.amount === null ? '未记录' : `${display(onlineProfitCorrection.amount)} ${onlineProfitCorrection.currency}` }}</dd></div>
            <div v-if="item.decision.reason_code === 'ready'"><dt>规则检查</dt><dd>规则检查通过</dd></div>
          </dl>
          <SkuPriceReferenceView :reference="skuPriceReference" :price-limit="priceLimit" />
          <details class="evidence">
            <summary>展开计算过程</summary>
            <dl v-if="traceRows.length" class="detail-grid">
              <div v-for="[key, value] in traceRows" :key="key"><dt>{{ traceLabels[key] || key }}</dt><dd>{{ display(value) }}</dd></div>
            </dl>
            <pre v-else>{{ display(item.decision.trace) }}</pre>
          </details>
        </section>
        <section v-if="!item.read_only" class="drawer-section">
          <h3>执行与证据</h3>
          <p class="muted">执行进度：{{ stateLabel(item.phase) }}</p>
          <details class="evidence"><summary>展开提交与回读记录</summary><pre>{{ display(item.execution) }}</pre></details>
          <details class="evidence"><summary>展开来源证据</summary><pre>{{ display(item.evidence) }}</pre></details>
        </section>
        <section v-if="admin && !item.read_only && item.status === '待处理'" class="drawer-section">
          <h3>重新评估</h3>
          <p class="muted">按当前规则重新获取来源数据并评估。</p>
          <el-input v-model="note" placeholder="补充处理说明（选填）" maxlength="500" />
          <el-button class="spaced" type="primary" :loading="busy" @click="emit('reevaluate', item.id, note)">重新评估</el-button>
        </section>
      </template>
    </div>
  </el-drawer>
</template>

