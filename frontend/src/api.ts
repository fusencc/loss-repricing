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

export interface ErpReconciliation {
  capture_id: string
  preview_id: string
  evaluated_at: string
  synthetic: boolean
}

export interface Batch {
  id: Identifier
  business_date: string
  mode: string
  status: string
  created_at: string
  summary: Record<string, unknown>
  error: string | null
  erp_reconciliation?: ErpReconciliation
  source_issue_count?: number
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
  read_only?: boolean
  control?: { mode: string; version: number; reason: string; handback_pending: boolean }
  ownership?: Ownership | null
  capabilities?: Record<string, boolean>
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
  kind?: string
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
    pending: '排队中', queued: '排队中', running: '运行中', pending_verification: '待核验', completed: '已完成',
    succeeded: '已完成', success: '已完成', failed: '失败', error: '失败',
    cancelled: '已取消', done: '已完成',
    proposed: '等待提交', blocked: '等待处理', excluded: '本轮不调',
    submitting: '正在提交', accepted: '已受理，待核验', unknown: '结果未知，继续核验',
    verified: '已核验', mismatch: '实际售价超差',
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

export function errorMessage(error: unknown): string {
  const message = baseErrorMessage(error)
  if (!axios.isAxiosError(error) || !error.response) return message
  const candidate: unknown = error.response.headers?.['x-error-id']
    ?? error.response.data?.error_id ?? error.response.headers?.['x-request-id']
  // 仅显示服务端生成的技术编号，不把异常正文或任意响应头拼接进提示。
  const identifier = typeof candidate === 'string' && /^[a-f0-9]{32}$/.test(candidate) ? candidate : null
  return identifier ? `${message}（排查编号：${identifier}）` : message
}

function baseErrorMessage(error: unknown): string {
  if (!axios.isAxiosError(error)) return error instanceof Error ? error.message : '操作失败，请重试。'
  if (!error.response) return '连接服务失败，请稍后重试。'
  const detail: unknown = error.response.data?.detail
  if (error.response.status === 409) return `记录已发生变化，请刷新后按最新版本重试。${typeof detail === 'string' ? detail : ''}`
  if (error.response.status === 403) return '当前账号没有该操作或商品范围的权限，请刷新归属后重试。'
  if (typeof detail === 'string' && detail) return detail
  if (Array.isArray(detail)) {
    return detail.map(entry => typeof entry?.msg === 'string' ? entry.msg : display(entry)).join('；')
  }
  return `操作失败（HTTP ${error.response.status}）。`
}

export interface Principal { id: string; username: string; display_name: string; role: 'admin' | 'manager' | 'sales'; can_correct: boolean }
export interface UserAccount extends Principal { active: boolean; dingtalk_userid?: string | null }
export interface Ownership { id: Identifier; platform: string; shop_id: string; site: string; target_id?: string | null; owner_id: string; owner_name?: string; supervisor_name?: string; supervisor_id?: string | null; autonomous: boolean }
export interface Correction { id: Identifier; desired_actual: string; reason: string; state: string; control_version: number; requested_by: string; created_at: string; result?: unknown }
export interface Notice { id: Identifier; recipient_id: string; batch_id: Identifier | null; item_ids: Identifier[]; summary: { title?: string; total?: number; exceptions?: number }; delivery_state: string; synthetic: boolean; ack_at: string | null; created_at: string; last_error?: string | null }
