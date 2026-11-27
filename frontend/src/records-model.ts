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
    vietnam_rounding_gap?: { amount: string; limit: string } | null
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
// 页面只展示三态；上传结果未知的记录并入待处理，保留明确的原因说明。
export const recordDisplayStates = ['已提价', '待处理', '不调价'] as const
export type RecordDisplayState = typeof recordDisplayStates[number]

function emptyRecordStatusCounts(): Record<RecordDisplayState, number> {
  return { '已提价': 0, '待处理': 0, '不调价': 0 }
}

export interface SellerGroup {
  id: string; name: string; assigned: boolean; items: RecordItem[]; latestPriced: RecordItem | null
  counts: Record<RecordDisplayState, number>
}

const sellerNameOrder = new Intl.Collator('zh-Hans-u-co-pinyin')

export function groupBySeller(items: RecordItem[], countRange?: BusinessDateRange): SellerGroup[] {
  const groups = new Map<string, SellerGroup>()
  for (const item of items) {
    const id = item.seller.id === null ? 'unassigned' : `seller:${item.seller.id}`
    let group = groups.get(id)
    if (!group) {
      group = { id, name: item.seller.name, assigned: item.seller.id !== null, items: [], latestPriced: null,
        counts: emptyRecordStatusCounts() }
      groups.set(id, group)
    }
    group.items.push(item)
    const status = recordStatusLabel(item)
    if (!countRange || (item.business_date >= countRange.start && item.business_date <= countRange.end)) {
      group.counts[status] += 1
    }
    // 接口已按记录更新从新到旧排列，跳过较新的待处理或不调价记录。
    if (status === '已提价') group.latestPriced ??= item
  }
  return [...groups.values()].sort((a, b) => {
    if (a.assigned !== b.assigned) return a.assigned ? -1 : 1
    const [aFirst = ''] = a.name
    const [bFirst = ''] = b.name
    return sellerNameOrder.compare(aFirst, bFirst) || sellerNameOrder.compare(a.name, b.name)
      || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)
  })
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

export interface BusinessDateRange {
  start: string; end: string
}

const businessDateFormat = new Intl.DateTimeFormat('sv-SE', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
})

export function todayBusinessDateRange(now = new Date()): BusinessDateRange {
  const today = businessDateFormat.format(now)
  return { start: today, end: today }
}

export function filterRecordBusinessDates(items: RecordItem[], { start, end }: BusinessDateRange): RecordItem[] {
  // 按所属批次归日，补核验不会把旧记录算到今天。
  return items.filter(item => item.business_date >= start && item.business_date <= end)
}

export function recordStatusLabel(item: RecordItem): RecordDisplayState {
  const execution = item.execution
  const state = execution?.state
  if (state === 'failed' || execution?.row_status === 'rejected' || execution?.submit_status === 'rejected') return '待处理'
  if (state === 'unknown' || state === 'submitting') return '待处理'
  if (item.status === '已提价') return '已提价'
  if (state === 'accepted' || state === 'uploaded' || state === 'verified' || execution?.submit_status === 'accepted') return '已提价'
  if (state === 'mismatch' || item.status === '待核验') return '待处理'
  return item.status === '不调价' ? '不调价' : '待处理'
}

export function countRecordStatuses(items: RecordItem[]): Record<RecordDisplayState, number> {
  const counts = emptyRecordStatusCounts()
  for (const item of items) counts[recordStatusLabel(item)] += 1
  return counts
}

const pendingReasonGuidance = new Map([
  ['候选明细未匹配到当前在线子 SKU', '系统没找到对应的 SKU'],
  ['平台、店铺、产品、商品、子 SKU 或仓配映射缺失', '系统没找到对应的 SKU'],
  ['站点价格档位导致实际价无效或超出误差容限', '系统算出的售价不符合站点规则'],
  ['本站点与仓配组合缺少已验证的单品上传和回读证据', '这个站点和发货仓暂不能自动改价'],
  ['提交前商品、仓配或现价与算价依据不一致', '改价前商品信息对不上仓'],
  ['提交前无法确认单品当前价格或操作日志', '售价或改价记录未确认'],
  ['提交前刷新后候选已消失', '复查时没找到待改价商品'],
])

// 待处理只显示固定的销售员提示，后台原因仅用于匹配。
export function recordStateReason(item: RecordItem): string {
  const status = recordStatusLabel(item)
  if (status === '待处理') {
    const state = item.execution?.state
    if (item.execution?.row_status === 'rejected' || item.execution?.submit_status === 'rejected') {
      return '改价未通过，请核对商品和要改的售价'
    }
    if (state === 'unknown' || state === 'submitting' || state === 'mismatch'
      || (item.status === '待核验' && state !== 'failed')) return '改价结果待确认，请到店铺后台查看，勿重复改价'
    if (state === 'failed') return '改价未提交成功，请把商品 ID 发给技术'
    const gap = item.decision?.vietnam_rounding_gap
    if (gap) {
      const amount = formatDecimal(gap.amount)
      const limit = formatDecimal(gap.limit)
      return amount === limit
        ? `取整后价差超过 ${limit} 越南盾（旧规则）`
        : `取整后与目标价约差 ${amount} 越南盾（旧上限 ${limit} 盾）`
    }
    const reason = item.reason?.trim().replace(/（排查编号 [^）]+）$/, '') ?? ''
    return pendingReasonGuidance.get(reason) ?? '暂未查明原因，请把商品 ID 发给技术'
  }
  if (status === '已提价') return '提价上传完成'
  const reason = item.reason
  if (typeof reason !== 'string' || !reason.trim()) return '原因未记录'
  // 只改写已明确保存官方条数的已知文案；无条数时不推断。
  const count = /^近72小时存在调价日志，不调价（官方记录 ([1-9]\d*) 条）$/.exec(reason)?.[1]
    ?? /^近三天存在([1-9]\d*)条调价日志$/.exec(reason)?.[1]
  return count ? `近3天存在${count}条调价` : reason
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

export function initialRecordDepartment(requested: string | null, saved: string | null): string {
  const known = (id: string | null) => recordDepartments.find(entry => entry.id === id)?.id
  return known(requested) ?? known(saved) ?? recordDepartments[0]!.id
}
