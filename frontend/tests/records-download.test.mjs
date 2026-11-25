import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { spawnSync } from 'node:child_process'
import { createSellerExcel, downloadSellerExcel, sellerExcelFilename, recordsExcelMime } from '../src/records-download.ts'
import { filterRecordBusinessDates } from '../src/records-model.ts'

const item = {
  key: { sku: '000123456789012345678901', item_id: '123456789012345678901', product_id: '000987654321012345678',
    shop_id: '001861287085550764034', site: 'PH', platform: 'Tiktok' },
  shop_name: null, first_margin: { amount: '-0.1234567890123456789', currency: 'CNY' },
  decision: { current_actual: '12.1234567890123456789', target_actual: '13.000000000000000001', trace: { station_currency: 'PHP' } },
  execution: { state: 'mismatch', submit_status: 'accepted', uploaded_price: '20.005000000000000001' }, status: '已提价',
  updated_at: '2026-11-23T01:00:00+00:00', synthetic: false,
}
const group = { name: '测试/销售', items: [item, { ...item, key: { ...item.key, sku: '=1+1', item_id: '' },
  shop_name: '店铺名称', first_margin: null, decision: null, execution: null, synthetic: true },
  { ...item, key: { ...item.key, product_id: undefined }, first_margin: { amount: '0', currency: 'CNY' } }] }

// 使用后端已有的独立 Excel 读取器，验证文件内容，不以文件名或同一写入库自读替代格式验收。
async function readWorkbook(blob, layout = false) {
  const python = new URL('../../.venv/bin/python', import.meta.url).pathname
  const result = spawnSync(python, ['-c', `
import io, json, sys
from openpyxl import load_workbook
wb = load_workbook(io.BytesIO(sys.stdin.buffer.read()))
if sys.argv[1] == 'layout':
 print(json.dumps({s.title: {
  'freeze': s.freeze_panes, 'grid': s.sheet_view.showGridLines,
  'widths': [s.column_dimensions[col].width for col in 'ABC'],
  'id_formats': [s[f'{col}2'].number_format for col in 'BC'],
  'header_fill': s['A1'].fill.fgColor.rgb, 'header_bold': s['A1'].font.bold,
  'header_height': s.row_dimensions[1].height, 'row_height': s.row_dimensions[2].height,
  'wrap': s['A2'].alignment.wrap_text,
  'alignment': [c.alignment.horizontal for c in s[2]],
  'stripe': s['A3'].fill.fgColor.rgb, 'status_fill': s['M2'].fill.fgColor.rgb,
  'status_bold': s['M2'].font.bold,
 } for s in wb}))
 sys.exit(0)
print(json.dumps({s.title: [[{'value': c.value, 'type': c.data_type} for c in row] for row in s] for s in wb}, ensure_ascii=False))
`, layout ? 'layout' : 'values'], { input: Buffer.from(await blob.arrayBuffer()), encoding: 'utf8' })
  assert.equal(result.status, 0, result.stderr)
  return JSON.parse(result.stdout)
}

test('personal export rounds only amount fields to two decimals and preserves IDs as text', async () => {
  const blob = await createSellerExcel(group, '测试部门')
  assert.equal(blob.type, recordsExcelMime)
  assert.deepEqual([...new Uint8Array(await blob.arrayBuffer()).slice(0, 4)], [0x50, 0x4b, 0x03, 0x04])
  const workbook = await readWorkbook(blob)
  assert.deepEqual(Object.keys(workbook), ['提价记录', '导出信息'])
  const rows = workbook['提价记录']
  assert.equal(rows.length, 4)
  assert.deepEqual(rows[0].map(c => c.value), ['商品编码（SKU）', '商品ID', '产品ID', '店铺名称／编号', '站点', '平台', '一次毛利金额',
    '毛利币种', '价格币种', '原营销价', '目标营销价', '上传价格', '记录状态', '更新时间（北京时间）', '是否模拟数据'])
  assert.deepEqual(rows[1].map(c => c.value), [item.key.sku, item.key.item_id, item.key.product_id, item.key.shop_id, 'PH', 'Tiktok',
    '-0.12', 'CNY', 'PHP', '12.12', '13', '20.01',
    '已提价', '2026-11-23 09:00:00', '否'])
  for (const index of [0, 1, 2, 3, 6, 9, 10, 11]) assert.equal(rows[1][index].type, 's')
  assert.deepEqual(rows[2].slice(0, 4).map(c => c.value), ['=1+1', null, item.key.product_id, '店铺名称'])
  assert.equal(rows[2][0].type, 's', 'formula-looking SKU must stay text')
  assert.deepEqual(rows[2].slice(6, 12).map(c => c.value), [null, null, null, null, null, null])
  assert.equal(rows[2][14].value, '是')
  assert.deepEqual(rows[3].slice(0, 3).map(c => c.value), [item.key.sku, item.key.item_id, null])
  assert.equal(rows[3][6].value, '0')
  assert.deepEqual(workbook['导出信息'].slice(0, 4).map(r => r.map(c => c.value)), [['导出项目', '内容'], ['销售员', group.name], ['部门', '测试部门'], ['记录总数', 3]])
})

test('empty export remains a readable workbook with headers and seller metadata', async () => {
  const workbook = await readWorkbook(await createSellerExcel({ ...group, items: [] }, '测试部门'))
  assert.equal(workbook['提价记录'].length, 1)
  assert.equal(workbook['导出信息'][1][1].value, group.name)
})

test('download uses XLSX filename and blob, clicks once, then releases object URL', async () => {
  assert.equal(sellerExcelFilename('a\\/:*?"<>|b'), 'a_________b-提价记录.xlsx')
  const original = { document: globalThis.document, window: globalThis.window,
    create: URL.createObjectURL, revoke: URL.revokeObjectURL }
  const link = { click() { this.clicked = (this.clicked ?? 0) + 1 } }
  let downloaded, released, cleanup
  globalThis.document = { createElement(tag) { assert.equal(tag, 'a'); return link } }
  globalThis.window = { setTimeout(callback, delay) { assert.equal(delay, 1000); cleanup = callback } }
  URL.createObjectURL = blob => { downloaded = blob; return 'blob:synthetic-test' }
  URL.revokeObjectURL = url => { released = url }
  try {
    await downloadSellerExcel(group, '测试部门')
    assert.equal(link.download, '测试_销售-提价记录.xlsx')
    assert.equal(link.href, 'blob:synthetic-test')
    assert.equal(link.clicked, 1)
    assert.equal(downloaded.type, recordsExcelMime)
    assert.equal(released, undefined)
    cleanup()
    assert.equal(released, 'blob:synthetic-test')
  } finally {
    globalThis.document = original.document
    globalThis.window = original.window
    URL.createObjectURL = original.create
    URL.revokeObjectURL = original.revoke
  }
})

test('RecordsPage exposes exact download hint and keeps seller-only click behavior', async () => {
  const source = await readFile(new URL('../src/RecordsPage.vue', import.meta.url), 'utf8')
  assert.match(source, /title="下载Excel" aria-label="下载Excel" @click\.stop="downloadSeller\(group\)"/)
  assert.match(source, /await downloadSellerExcel\(group, departmentName\.value, range\)/)
  assert.match(source, /Excel 下载未成功，请重试。/)
  assert.doesNotMatch(source, /下载个人数据|提价记录\.json|JSON\.stringify/)
})

test('dialog downloads the same date slice as the list without applying status or keyword filters', async () => {
  const source = await readFile(new URL('../src/RecordsPage.vue', import.meta.url), 'utf8')
  assert.match(source, /downloadSeller\(\{ name: activeSeller\.name, items: logItems \}, logRange\)/)
  assert.match(source, /<SellerRecordLogs[^>]*:items="logItems"/)
  assert.match(source, /:disabled="!logItems.length"/)

  const range = { start: '2026-11-24', end: '2026-11-25' }
  const items = group.items.map((record, index) => ({ ...record, business_date: `2026-11-${23 + index}` }))
  const workbook = await readWorkbook(await createSellerExcel({ ...group, items: filterRecordBusinessDates(items, range) }, '测试部门', range))
  assert.deepEqual(workbook['提价记录'].slice(1).map(row => row.slice(0, 3).map(c => c.value)), [
    ['=1+1', null, item.key.product_id],
    [item.key.sku, item.key.item_id, null],
  ])
  assert.match(workbook['导出信息'].find(row => row[0].value === '数据范围')[1].value, /批次业务日期（北京时间）：2026-11-24至2026-11-25/)
  assert.equal(sellerExcelFilename(group.name, range), '测试_销售-提价记录-2026-11-24至2026-11-25.xlsx')
  assert.equal(sellerExcelFilename(group.name, { start: range.start, end: range.start }), '测试_销售-提价记录-2026-11-24.xlsx')
})


test('export layout keeps headers and identities visible with readable styled rows', async () => {
  const workbook = await readWorkbook(await createSellerExcel(group, '测试部门'), true)
  const sheet = workbook['提价记录']
  assert.equal(sheet.freeze, 'C2')
  assert.equal(sheet.grid, false)
  assert.deepEqual(sheet.widths, [32, 28, 28])
  assert.deepEqual(sheet.id_formats, ['@', '@'])
  assert.equal(sheet.header_fill, 'FF243B53')
  assert.equal(sheet.header_bold, true)
  assert.equal(sheet.header_height, 34)
  assert.equal(sheet.row_height, 36)
  assert.equal(sheet.wrap, true)
  assert.deepEqual(sheet.alignment, ['left', 'left', 'left', 'left', 'center', 'left', 'right', 'center', 'center',
    'right', 'right', 'right', 'center', 'left', 'center'])
  assert.equal(sheet.stripe, 'FFF0F4F8')
  assert.equal(sheet.status_fill, 'FFE6F4EA')
  assert.equal(sheet.status_bold, true)
  assert.equal(workbook['导出信息'].freeze, 'A2')
})

test('export converts explicit time zones across Beijing midnight without guessing missing time zones', async () => {
  const items = ['2026-11-23T17:30:00Z', '2026-11-24T09:30:00+08:00', '2026-11-23 01:00:00', 'invalid', null]
    .map(updated_at => ({ ...item, updated_at }))
  const workbook = await readWorkbook(await createSellerExcel({ ...group, items }, '测试部门'))
  assert.deepEqual(workbook['提价记录'].slice(1).map(row => row[13].value), [
    '2026-11-24 01:30:00', '2026-11-24 09:30:00', '2026-11-23 01:00:00', 'invalid', null,
  ])
})
