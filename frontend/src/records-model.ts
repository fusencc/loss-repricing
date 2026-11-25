import { formatDecimal } from './decimal-display.ts'
import type { ItemStatus, SkuPriceReference, PriceLimit } from './api'

export interface RecordStage {
  key: 'original' | 'loss' | 'break_even'
  label: string
  price: string | null
  cny: string | null
  change_cny: string | null
  change_percent: string | null
  // ③ 利润校验行专属：在线利润校正额折合人民币；缺证据为 null，不推断为 0。
  profit_change_cny: string | null
  // 仅在②有初步补亏后营销价的试算记录时提供；百分数已经由后端换算。
  trial_profit?: string
  trial_margin_percent?: string
}
export interface RecordStages {
  currency: string | null
  fx_rate: string | null
  fx_date: string | null
  rows: RecordStage[]
}
export interface RecordItem {
  process?: { id: string; updated_at: string; stage: string; reason: string }[]
  id: string; batch_id: string; key: Record<string, string>
  seller: { id: string | null; name: string }; department: string | null; shop_name: string | null
  status: ItemStatus; phase?: string | null; reason: string; updated_at: string; display_time: string | null; business_date: string; mode: string; synthetic: boolean
  first_margin: { amount: string; currency: string } | null
  decision: { current_actual: string | null; target_actual: string | null; upload_price: string | null
    price_change: string | null
    stages: RecordStages | null
    sku_price_reference?: SkuPriceReference | null
    price_limit?: PriceLimit | null
    trace: { station_currency: string | null } } | null
  corrected_first_margin: { amount: string; currency: string } | null
  execution: { state: string; submit_status?: string | null; row_status?: string | null; task_id: string | null; uploaded_price: string | null; actual_price: string | null
    confirmed_at: string | null; effective_at: string | null; effective_time_source: string | null; updated_at: string } | null
}
export interface RecordData {
  scope_issues: { salesperson: string; department: string; business_date: string; capture_id: string; reason: string }[]
  departments: { id: string; name: string; total: number }[]; department: string | null
  total: number; shops: number; counts: Partial<Record<ItemStatus, number>>; items: RecordItem[]
}
// 用户 2026-11-25 明确要求：待核验直接归类为待处理，页面只保留三态。
// 受理、提交中、结果未知等未终结证据仍按原执行状态保存在记录中，由原因说明区分。
export const recordDisplayStates = ['已提价', '待处理', '不调价'] as const
export type RecordDisplayState = typeof recordDisplayStates[number]

function emptyRecordStatusCounts(): Record<RecordDisplayState, number> {
  return { '已提价': 0, '待处理': 0, '不调价': 0 }
}

export interface SellerGroup {
  id: string; name: string; assigned: boolean; items: RecordItem[]; latest: RecordItem
  counts: Record<RecordDisplayState, number>
}

export function groupBySeller(items: RecordItem[]): SellerGroup[] {
  const groups = new Map<string, SellerGroup>()
  for (const item of items) {
    const id = item.seller.id === null ? 'unassigned' : `seller:${item.seller.id}`
    let group = groups.get(id)
    if (!group) {
      group = { id, name: item.seller.name, assigned: item.seller.id !== null, items: [], latest: item,
        counts: emptyRecordStatusCounts() }
      groups.set(id, group)
    }
    group.items.push(item)
    group.counts[recordStatusLabel(item)] += 1
  }
  return [...groups.values()]
}

export function recordPrice(value: string | null | undefined, item: RecordItem): string {
  if (value == null) return '未记录'
  return `${formatDecimal(value)} ${item.decision?.trace.station_currency ?? '币种未记录'}`
}

export function recordSource(item: RecordItem): string {
  if (item.synthetic) return '模拟数据'
  return ({ file: '文件对账', live: '真实接口记录', demo: '模拟数据' }[item.mode] ?? '来源未标注')
}

export function recordTone(status: RecordDisplayState): string {
  return ({ '已提价': 'verified', '待处理': 'attention', '不调价': 'neutral' }[status])
}

export function effectiveTimeLabel(source: string | null | undefined): string {
  if (source === 'effective_at') return '来源记录的生效时间'
  if (source === 'confirmed_at') return '保护起算时间（按首次核验）'
  return '保护起算时间（来源未记录）'
}

// 标题日期只投影保存的执行证据。首次核验不是平台真实生效，更新时间也不是提交或生效时间。
// 与 timeLabel 一致使用北京时间及中文年月日格式；无时区、无效或缺失的时间不猜今天。
function recordDateEvidence(items: RecordItem[]): { dates: Map<string, string[]>; missing: boolean } {
  const dates = new Map<string, string[]>()
  const format = new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  })
  const dateText = (value: string | null | undefined): string | null => {
    if (!value || !/T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return null
    const date = new Date(value)
    return Number.isNaN(date.getTime()) ? null : format.format(date)
  }
  let missing = items.length === 0
  for (const item of items) {
    const execution = item.execution
    const effective = item.status === '已核验' && execution?.effective_time_source === 'effective_at'
      ? dateText(execution.effective_at) : null
    const confirmed = item.status === '已核验' ? dateText(execution?.confirmed_at) : null
    const updated = dateText(item.updated_at)
    const label = effective ? '提价生效日期' : confirmed ? '提价核验日期' : '记录更新日期'
    const date = effective ?? confirmed ?? updated
    if (!date) { missing = true; continue }
    const group = dates.get(label) ?? []
    group.push(date)
    dates.set(label, group)
  }
  return { dates, missing }
}

// 完整说明只用于标题悬浮提示，保留日期性质、范围与北京时间口径。
export function recordDateLabel(items: RecordItem[]): string {
  const { dates, missing } = recordDateEvidence(items)
  const labels = ['提价生效日期', '提价核验日期', '记录更新日期'].flatMap(label => {
    const sorted = [...new Set(dates.get(label) ?? [])].sort()
    if (!sorted.length) return []
    return [`${label}：${sorted[0]}${sorted.length > 1 ? ` 至 ${sorted[sorted.length - 1]}` : ''}`]
  })
  if (missing) labels.push('提价日期未记录')
  return `${labels.join('；')}${dates.size ? '（北京时间）' : ''}`
}

// 标题只展示一个最近日期：同日只显示一次，范围取最新端，不额外标注日期性质。
export function recordDateValue(items: RecordItem[]): string {
  const { dates } = recordDateEvidence(items)
  const all = [...dates.values()].flat().sort()
  return all.length ? all[all.length - 1]! : '日期未记录'
}

export function recordStatusLabel(item: RecordItem): RecordDisplayState {
  // 已提价只投影已落库的核验成功状态；受理回执不能覆盖后续核验失败。
  const state = item.execution?.state
  if (item.status === '已核验' && state === 'verified') return '已提价'
  // 待核验并入待处理展示；具体处于受理、提交中还是结果未知，由 recordStateReason 说明。
  if (state === 'mismatch' || state === 'failed') return '待处理'
  if (state === 'accepted' || state === 'unknown' || state === 'submitting' || item.status === '待核验') return '待处理'
  return item.status === '不调价' ? '不调价' : '待处理'
}

export function countRecordStatuses(items: RecordItem[]): Record<RecordDisplayState, number> {
  const counts = emptyRecordStatusCounts()
  for (const item of items) counts[recordStatusLabel(item)] += 1
  return counts
}

// 类别只使用本条持久执行阶段及回执类型；原始原因完整保留，查询失败未持久化时不能猜测。
export function recordStateReason(item: RecordItem): string {
  if (recordStatusLabel(item) === '已提价') return '实际有效价已回读核验成功'
  const reason = typeof item.reason === 'string' && item.reason.trim() ? item.reason : '原因未记录'
  if (item.status !== '待处理' && item.status !== '待核验') return reason
  const execution = item.execution
  let category = ''
  if (execution?.state === 'accepted') category = '上传已受理，待回读核验'
  else if (execution?.state === 'unknown') category = '提交结果未知，仅核验、不自动重传'
  else if (execution?.state === 'submitting') category = '提交中或结果未确认，仅核验、不自动重传'
  else if (execution?.state === 'mismatch') category = '回读核验未通过'
  else if (execution?.state === 'failed' && execution.row_status === 'rejected') category = '逐行处理失败'
  else if (execution?.state === 'failed' && execution.submit_status === 'rejected') category = '提交被拒绝'
  else if (execution?.state === 'failed' && execution.submit_status === 'not_submitted') category = '未提交'
  else if (!execution && item.phase === 'proposed') category = '待提交'
  // 历史代码在空回执消息时统一写入受理文案；其他状态不能以此证明受理，仍保留原文供核对。
  if (execution && execution.state !== 'accepted' && reason === '上传已受理，等待实际价格回读') {
    return `${category || '执行结果未明确'}；原记录说明（可能为默认文案）：${reason}`
  }
  return category && reason !== category ? `${category}；${reason}` : reason
}

// 价格变动展示：后端已给出 target_actual − current_actual 的十进制文本；这里只决定符号和空值文案。
// 数字前带 + 表示调价引起的增加，负数保留原始负号，零不显示加号。
export function isPositiveChange(item: RecordItem): boolean {
  return (item.decision?.price_change ?? '').startsWith('+')
}

export function changeLabel(item: RecordItem): string {
  const raw = item.decision?.price_change
  if (raw == null) return '未记录'
  const change = formatDecimal(raw)
  if (change.startsWith('-') || change.startsWith('+')) return `${change} ${item.decision?.trace.station_currency ?? ''}`.trim()
  return `+${change} ${item.decision?.trace.station_currency ?? ''}`.trim()
}

// 三环节价格表的展示格式：后端已按保存金额和当日汇率算好，这里只补单位与符号。
// 增加为正时前置 +，为负保留 -，零不虚加号；缺值统一显示 —，不推断为 0。
export function signedDecimal(value: string | null | undefined): string {
  if (value == null || value === '') return '—'
  const shown = formatDecimal(value)
  if (/^[-+]?0(\.0+)?$/.test(shown)) return shown.replace(/^\+/, '')
  return shown.startsWith('-') || shown.startsWith('+') ? shown : `+${shown}`
}

// 增加为正时用强调色；零与负值保持普通色。
export function isIncrease(value: string | null | undefined): boolean {
  if (value == null || value === '') return false
  if (value.startsWith('-')) return false
  return !/^[-+]?0(\.0+)?$/.test(value)
}

// 只按原始十进制文本识别负数，避免精度转换以及将缺失或负零误认为亏损。
export function isNegativeAmount(value: string | null | undefined): boolean {
  return typeof value === 'string' && /^-\d+(?:\.\d+)?$/.test(value) && /[1-9]/.test(value)
}

// 日志名称只在展示层调整；保留后台阶段键、金额和「参考价优先」等证据语义，不回写历史。
export function recordStageLabel(stage: RecordStage): string {
  const label = stage.label.replace(/^[\u2460-\u2462]\s*/, '')
  return ({ '原实际营销价': '原营销价', '补亏损': '需补亏损', '利润校验': '利润校验变动' }[label] ?? label)
}

export function stagePriceLabel(stage: RecordStage, currency: string | null | undefined): string {
  if (stage.price == null) return '未记录'
  return `${formatDecimal(stage.price)} ${currency ?? '币种未记录'}`
}

export function stageChangeLabel(value: string | null | undefined): string {
  return value == null ? '—' : `${signedDecimal(value)} 元`
}

export function stagePercentLabel(value: string | null | undefined): string {
  return value == null ? '—' : `${signedDecimal(value)}%`
}

// 弹窗筛选只合并显示分组，不修改记录原状态或执行动作；关键词匹配 SKU、商品/子商品 ID、店铺名与店铺 ID。
export type RecordStatusFilter = RecordDisplayState | '全部'

export function filterRecordItems(items: RecordItem[], status: RecordStatusFilter, query: string): RecordItem[] {
  const keyword = query.trim().toLocaleLowerCase()
  return items.filter(item => {
    if (status !== '全部' && recordStatusLabel(item) !== status) return false
    if (!keyword) return true
    const haystack = [item.key.sku, item.key.product_id, item.key.item_id,
      item.shop_name, item.key.shop_id].filter(Boolean).join('\n').toLocaleLowerCase()
    return haystack.includes(keyword)
  })
}

export const recordDepartments = ['Tiktok一部', 'Tiktok五部', 'Tiktok十二部', 'Tiktok二十二部'].map(name => ({ id: 'department:' + name, name }))
