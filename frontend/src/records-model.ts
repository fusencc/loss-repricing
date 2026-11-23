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
  status: ItemStatus; reason: string; updated_at: string; business_date: string; mode: string; synthetic: boolean
  first_margin: { amount: string; currency: string } | null
  decision: { current_actual: string | null; target_actual: string | null; upload_price: string | null
    price_change: string | null
    stages: RecordStages | null
    sku_price_reference?: SkuPriceReference | null
    price_limit?: PriceLimit | null
    trace: { station_currency: string | null } } | null
  corrected_first_margin: { amount: string; currency: string } | null
  execution: { state: string; task_id: string | null; uploaded_price: string | null; actual_price: string | null
    confirmed_at: string | null; effective_at: string | null; effective_time_source: string | null; updated_at: string } | null
}
export interface RecordData {
  departments: { id: string; name: string; total: number }[]; department: string | null
  total: number; shops: number; counts: Partial<Record<ItemStatus, number>>; items: RecordItem[]
}
export interface SellerGroup {
  id: string; name: string; assigned: boolean; items: RecordItem[]; latest: RecordItem
  counts: Record<ItemStatus, number>
}

export function groupBySeller(items: RecordItem[]): SellerGroup[] {
  const groups = new Map<string, SellerGroup>()
  for (const item of items) {
    const id = item.seller.id === null ? 'unassigned' : `seller:${item.seller.id}`
    let group = groups.get(id)
    if (!group) {
      group = { id, name: item.seller.name, assigned: item.seller.id !== null, items: [], latest: item,
        counts: { '已核验': 0, '待核验': 0, '待处理': 0, '本轮不调': 0 } }
      groups.set(id, group)
    }
    group.items.push(item)
    group.counts[item.status] += 1
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

export function recordTone(status: string): string {
  return ({ '已核验': 'verified', '待核验': 'attention', '待处理': 'attention' }[status] ?? 'neutral')
}

export function effectiveTimeLabel(source: string | null | undefined): string {
  if (source === 'effective_at') return '来源记录的生效时间'
  if (source === 'confirmed_at') return '保护起算时间（按首次核验）'
  return '保护起算时间（来源未记录）'
}

export function recordStatusLabel(status: string): string {
  // 仅合并前端展示：后台待核验仍只核验、不重传，原始状态和执行证据保持不变。
  return ({ '已核验': '已提价', '待核验': '待处理', '待处理': '待处理' }[status] ?? status)
}

// 仅展示本条已保存的原因，不从销量、历史尝试或价格推导新的保护结论；空值明确未记录。
export function recordStateReason(item: RecordItem): string {
  if (item.status === '已核验') return '商品符合调价'
  return typeof item.reason === 'string' && item.reason.trim() ? item.reason : '原因未记录'
}

export const recordDisplayStates = ['已核验', '待处理', '本轮不调'] as const

// 汇总的是记录数量，不计算价格；同一显示状态合并后台两个互斥状态，避免隐藏待核验后漏计。
export function recordStatusCount(counts: Partial<Record<ItemStatus, number>>, status: typeof recordDisplayStates[number]): number {
  return status === '待处理' ? (counts['待处理'] ?? 0) + (counts['待核验'] ?? 0) : counts[status] ?? 0
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

// 日志名称只在展示层调整；保留后台阶段键、金额和「参考价优先」等证据语义，不回写历史。
export function recordStageLabel(stage: RecordStage): string {
  const label = stage.label.replace(/^[\u2460-\u2462]\s*/, '')
  return ({ '补亏损': '补毛利亏', '利润校验': '利润校验变动' }[label] ?? label)
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
export type RecordStatusFilter = typeof recordDisplayStates[number] | '全部'

export function filterRecordItems(items: RecordItem[], status: RecordStatusFilter, query: string): RecordItem[] {
  const keyword = query.trim().toLocaleLowerCase()
  return items.filter(item => {
    if (status !== '全部' && recordStatusLabel(item.status) !== recordStatusLabel(status)) return false
    if (!keyword) return true
    const haystack = [item.key.sku, item.key.product_id, item.key.item_id,
      item.shop_name, item.key.shop_id].filter(Boolean).join('\n').toLocaleLowerCase()
    return haystack.includes(keyword)
  })
}

export function filterRecordItemsCount(items: RecordItem[], status: RecordStatusFilter, query: string): number {
  return filterRecordItems(items, status, query).length
}

export const recordDepartments = ['Tiktok一部', 'Tiktok五部', 'Tiktok十二部', 'Tiktok二十二部'].map(name => ({ id: 'department:' + name, name }))
