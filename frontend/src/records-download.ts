import type { SellerGroup } from './records-model'
import type { Cell, SheetData } from 'write-excel-file/universal'

export const recordsExcelMime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

const text = (value: string | null | undefined): Cell => value == null ? null : { type: String, value }

export function sellerExcelFilename(name: string): string {
  return `${name.replace(/[\\/:*?"<>|]/g, '_')}-提价记录.xlsx`
}

export async function createSellerExcel(group: SellerGroup, department: string): Promise<Blob> {
  // 仅导出当前 /records 已授权读取的该销售员全部记录，不另取数据、不受页面姓名搜索影响。
  // 字段沿用原个人 JSON 导出；毛利对象展开为金额和币种，人员与部门保存在独立信息表。
  // ID、SKU 和十进制金额显式写为文本，保留前导零及超过 Excel 15 位的精度；不算价、
  // 不将目标价冒充回读价，不改原始状态。null 留空，确证零和模拟标记 false 保留原值。
  const headers = ['sku', 'shop', 'site', 'platform', 'first_margin.amount', 'first_margin.currency',
    'currency', 'current_price', 'target_price', 'uploaded_price', 'status', 'updated_at', 'synthetic']
  const rows: SheetData = [headers.map(text), ...group.items.map(item => [
    text(item.key.sku), text(item.shop_name || item.key.shop_id), text(item.key.site), text(item.key.platform),
    text(item.first_margin?.amount), text(item.first_margin?.currency),
    text(item.decision?.trace.station_currency), text(item.decision?.current_actual),
    text(item.decision?.target_actual), text(item.execution?.uploaded_price),
    text(item.status), text(item.updated_at), { type: Boolean, value: item.synthetic },
  ])]
  // 懒加载文件写入库，普通首页读取不承担 Excel 生成器的加载开销。
  const { default: writeXlsxFile } = await import('write-excel-file/universal')
  return writeXlsxFile([
    { data: rows, sheet: '提价记录' },
    { data: [[text('seller'), text(group.name)], [text('department'), text(department)]], sheet: '导出信息' },
  ]).toBlob()
}

export async function downloadSellerExcel(group: SellerGroup, department: string): Promise<void> {
  const file = await createSellerExcel(group, department)
  const url = URL.createObjectURL(file)
  const link = document.createElement('a')
  link.href = url
  link.download = sellerExcelFilename(group.name)
  try {
    link.click()
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
}
