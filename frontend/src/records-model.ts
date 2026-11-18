import type { ItemStatus } from './api'

export interface RecordItem {
  process?: { id: string; updated_at: string; stage: string; reason: string }[]
  id: string; batch_id: string; key: Record<string, string>
  seller: { id: string | null; name: string }; department: string | null; shop_name: string | null
  status: ItemStatus; reason: string; updated_at: string; business_date: string; mode: string; synthetic: boolean
  first_margin: { amount: string; currency: string } | null
  decision: { current_actual: string | null; target_actual: string | null; upload_price: string | null
    trace: { station_currency: string | null } } | null
  execution: { state: string; task_id: string | null; uploaded_price: string | null; actual_price: string | null
    confirmed_at: string | null; effective_at: string | null; effective_time_source: string | null; updated_at: string
    prices: Partial<Record<'uploaded_tax_inclusive' | 'tax_exclusive' | 'actual_tax_inclusive' | 'marketing',
      { amount: string | null; currency: string | null; observed_at: string | null }>> } | null
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
  return `${value} ${item.decision?.trace.station_currency ?? '币种未记录'}`
}

export function recordSource(item: RecordItem): string {
  if (item.synthetic) return '模拟数据'
  return ({ file: '文件对账', live: '真实接口记录', demo: '模拟数据' }[item.mode] ?? '来源未标注')
}

export function recordTone(status: string): string {
  return ({ '已核验': 'verified', '待核验': 'pending', '待处理': 'attention' }[status] ?? 'neutral')
}

export function effectiveTimeLabel(source: string | null | undefined): string {
  if (source === 'effective_at') return '来源记录的生效时间'
  if (source === 'confirmed_at') return '保护起算时间（按首次核验）'
  return '保护起算时间（来源未记录）'
}

export function recordStatusLabel(status: string): string {
  return ({ '已核验': '已提价', '待核验': '待处理', '待处理': '活动中' }[status] ?? status)
}

export const recordDepartments = ['Tiktok一部', 'Tiktok五部', 'Tiktok十二部', 'Tiktok二十二部'].map(name => ({ id: 'department:' + name, name }))
