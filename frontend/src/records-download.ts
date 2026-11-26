import { formatDecimal } from './decimal-display.ts'
import { recordStatusLabel } from './records-model.ts'
import type { BusinessDateRange, SellerGroup } from './records-model'
import type { Cell, SheetData } from 'write-excel-file/universal'

export const recordsExcelMime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

const text = (value: string | null | undefined): Cell => value == null ? null : { type: String, value, format: '@' }
const amount = (value: string | null | undefined): Cell => text(value == null ? value : formatDecimal(value))
const headers = ['商品编码（SKU）', '商品ID', '产品ID', '店铺名称／编号', '站点', '平台', '一次毛利金额', '毛利币种',
  '价格币种', '原营销价', '目标营销价', '上传价格', '记录状态', '更新时间（北京时间）']
const widths = [32, 28, 28, 32, 12, 16, 30, 12, 12, 30, 30, 30, 14, 25]
const statusColumn = 12
const headerCell = (value: string): Cell => ({ type: String, value, fontWeight: 'bold',
  backgroundColor: '#243B53', textColor: '#FFFFFF', height: 34, align: 'center', alignVertical: 'center', wrap: true })

// 只转换有明确时区的有效时间，缺失留空；格式异常保留来源文本，不猜测时区。
function beijingTime(value: string | null | undefined): string | null {
  if (!value) return null
  const date = new Date(value)
  if (!/T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value) || Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit',
    day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).format(date)
}

type SellerExport = Pick<SellerGroup, 'name' | 'items'>

function rangeLabel({ start, end }: BusinessDateRange): string {
  return start === end ? start : `${start}至${end}`
}

export function sellerExcelFilename(name: string, range?: BusinessDateRange): string {
  return `${name.replace(/[\\/:*?"<>|]/g, '_')}-提价记录${range ? `-${rangeLabel(range)}` : ''}.xlsx`
}

export async function createSellerExcel(group: SellerExport, department: string, range?: BusinessDateRange): Promise<Blob> {
  // 导出页面传入的记录，不另取数据或算价。
  // 编号与金额仍以文本写入，避免 Excel 截断长编号或大额金额。
  // 上传价格不代表已生效价格；缺失留空、确证零保留。
  const rows: SheetData = [headers.map(headerCell), ...group.items.map((item, index) => {
    const status = recordStatusLabel(item)
    const cells: Cell[] = [
      text(item.key.sku), text(item.key.item_id || null), text(item.key.product_id || null),
      text(item.shop_name || item.key.shop_id), text(item.key.site), text(item.key.platform),
      amount(item.first_margin?.amount), text(item.first_margin?.currency),
      text(item.decision?.trace.station_currency), amount(item.decision?.current_actual),
      amount(item.decision?.target_actual), amount(item.execution?.uploaded_price),
      text(status), text(beijingTime(item.updated_at)),
    ]
    return cells.map((cell, column): Cell => {
      const styled: Cell = { ...(cell ?? { type: String, value: '' }), height: 36,
        backgroundColor: index % 2 ? '#F0F4F8' : '#FFFFFF', textColor: '#243B53',
        bottomBorderColor: '#D9E2EC', bottomBorderStyle: 'hair', alignVertical: 'center', wrap: true,
        align: [6, 9, 10, 11].includes(column) ? 'right' : [4, 7, 8, statusColumn].includes(column) ? 'center' : 'left' }
      if (column === statusColumn) {
        styled.fontWeight = 'bold'
        styled.textColor = status === '已提价' ? '#13795B' : status === '不调价' ? '#52606D' : '#9A6700'
        styled.backgroundColor = status === '已提价' ? '#E6F4EA' : status === '不调价' ? '#E8EDF2' : '#FFF4CE'
      }
      return styled
    })
  })]
  const info: SheetData = [
    ['导出项目', '内容'].map(headerCell),
    [text('销售员'), text(group.name)], [text('部门'), text(department)],
    [text('记录总数'), { type: Number, value: group.items.length }],
    [text('时间口径'), text('更新时间按北京时间显示；无法识别的时间保留来源文本。')],
    [text('价格说明'), text('原营销价、目标营销价及上传价格均沿用已保存记录；上传价格不等于实际生效价。')],
    [text('币种说明'), text('一次毛利对应毛利币种；三列价格对应价格币种，保留标准币种代码。')],
    [text('精度说明'), text('商品编码、商品ID、产品ID和店铺编号按原文保存；金额四舍五入至最多两位小数，空白表示未记录。')],
    [text('数据范围'), text(range
      ? `批次业务日期（北京时间）：${rangeLabel(range)}。含所选日期内全部记录，不受状态或关键词筛选影响。`
      : '该销售员当前可读取的全部记录，不受页面搜索条件影响。')],
  ].map((row, index) => index === 0 ? row : row.map((cell, column): Cell => ({ ...cell,
    height: index > 3 ? 48 : 28, wrap: true, alignVertical: 'center',
    fontWeight: column === 0 ? 'bold' : undefined,
    backgroundColor: index % 2 ? '#F0F4F8' : '#FFFFFF', textColor: '#243B53' })))
  // 懒加载生成器，首页普通读取无需加载 Excel 写入库。
  const { default: writeXlsxFile } = await import('write-excel-file/universal')
  return writeXlsxFile([
    { data: rows, sheet: '提价记录', columns: widths.map(width => ({ width })),
      stickyRowsCount: 1, stickyColumnsCount: 2, showGridLines: false, orientation: 'landscape' },
    { data: info, sheet: '导出信息', columns: [{ width: 18 }, { width: 88 }],
      stickyRowsCount: 1, showGridLines: false },
  ], { fontFamily: 'Microsoft YaHei', fontSize: 11 }).toBlob()
}

export async function downloadSellerExcel(group: SellerExport, department: string, range?: BusinessDateRange): Promise<void> {
  const file = await createSellerExcel(group, department, range)
  const url = URL.createObjectURL(file)
  const link = document.createElement('a')
  link.href = url
  link.download = sellerExcelFilename(group.name, range)
  try {
    link.click()
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
}
