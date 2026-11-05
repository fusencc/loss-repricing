import axios from 'axios'

export const api = axios.create({ baseURL: '/api', withCredentials: true, timeout: 20000 })

export const statuses = ['本轮不调', '待处理', '待核验', '已核验'] as const
export type ItemStatus = typeof statuses[number]
export type Identifier = string | number

export interface Settings {
  paused: boolean
  paused_sites: string[]
  pilot_shops: string[]
  autonomous_enabled: boolean
  site_enabled: string[]
  schedule_enabled: boolean
}

export interface Batch {
  id: Identifier
  business_date: string
  mode: string
  status: string
  created_at: string
  summary: Record<string, unknown>
  error: string | null
}

export interface Item {
  id: Identifier
  batch_id: Identifier
  key: Record<string, string> & {
    platform: string
    shop_id: string
    site: string
    product_id: string
    item_id: string
    sku: string
    warehouse: string
  }
  status: ItemStatus
  phase: string
  reason: string
  decision: {
    ready: boolean
    reason_code: string
    current_actual: string | null
    discount: string | null
    unit_loss: string | null
    loss_currency: string | null
    station_loss: string | null
    target_actual: string | null
    upload_price: string | null
    predicted_profit: string | null
    selected_row_id: string | null
    source_row_ids: string[]
    trace: unknown
  } | null
  execution: Record<string, unknown> | null
  evidence?: unknown
  updated_at: string
}

export interface Overview {
  mode: string
  counts: Record<ItemStatus, number>
  batch_count: number
  latest_batch: Batch | null
  worker: { heartbeat: string | null, last_error: string | null }
  settings: Settings
  readiness: { live_enabled: boolean, contract_ready: boolean, missing: string[] }
  delayed: boolean
}

export interface Contracts {
  mode: string
  verified_sites: string[]
  missing: string[]
  source_versions: Record<string, unknown>
}

export interface Protection {
  id: Identifier
  shop_id: string
  site: string
  main_sku: string
  reason: string
  active: boolean
  created_at: string
}

export interface Job {
  id: Identifier
  state: string
  error: string | null
  batch_id: Identifier | null
}

export function statusType(status: ItemStatus) {
  return {
    本轮不调: 'info',
    待处理: 'warning',
    待核验: 'primary',
    已核验: 'success',
  }[status] as 'info' | 'warning' | 'primary' | 'success'
}

export function modeLabel(mode: string) {
  return { demo: '演示数据', file: '文件对账', live: '真实接口' }[mode] ?? mode
}

export function stateLabel(state: string) {
  return {
    pending: '排队中', queued: '排队中', running: '运行中', completed: '已完成',
    succeeded: '已完成', success: '已完成', failed: '失败', error: '失败',
    cancelled: '已取消', done: '已完成',
  }[state.toLowerCase()] ?? state
}

export function display(value: unknown): string {
  if (value == null || value === '') return '—'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (typeof value === 'object') return JSON.stringify(value, null, 2)
  return String(value)
}

export function timeLabel(value: string | null | undefined) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit',
    day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
  }).format(date)
}

export function errorMessage(error: unknown) {
  if (!axios.isAxiosError(error)) return error instanceof Error ? error.message : '操作失败，请重试。'
  if (!error.response) return '连接服务失败，请稍后重试。'
  const detail: unknown = error.response.data?.detail
  if (typeof detail === 'string' && detail) return detail
  if (Array.isArray(detail)) {
    return detail.map(entry => typeof entry?.msg === 'string' ? entry.msg : display(entry)).join('；')
  }
  return `操作失败（HTTP ${error.response.status}）。`
}
