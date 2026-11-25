import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { spawnSync } from 'node:child_process'
import { createSellerExcel, downloadSellerExcel, sellerExcelFilename, recordsExcelMime } from '../src/records-download.ts'

const item = {
  key: { sku: '000123456789012345678901', shop_id: '001861287085550764034', site: 'PH', platform: 'Tiktok' },
  shop_name: null, first_margin: { amount: '-0.1234567890123456789', currency: 'CNY' },
  decision: { current_actual: '12.1234567890123456789', target_actual: '13.000000000000000001', trace: { station_currency: 'PHP' } },
  execution: { uploaded_price: '20.000000000000000001' }, status: '待核验',
  updated_at: '2026-11-23T01:00:00+00:00', synthetic: false,
}
const group = { name: '测试/销售', items: [item, { ...item, key: { ...item.key, sku: '=1+1' },
  shop_name: '店铺名称', first_margin: null, decision: null, execution: null, synthetic: true },
  { ...item, first_margin: { amount: '0', currency: 'CNY' } }] }

// 使用后端已有的独立 Excel 读取器，验证文件内容，不以文件名或同一写入库自读替代格式验收。
async function readWorkbook(blob, layout = false) {
  const python = new URL('../../.venv/bin/python', import.meta.url).pathname
  const result = spawnSync(python, ['-c', `
import io, json, sys
from openpyxl import load_workbook
wb = load_workbook(io.BytesIO(sys.stdin.buffer.read()))
if sys.argv[1] == 'layout':
 print(json.dumps({s.title: {'freeze': s.freeze_panes, 'grid': s.sheet_view.showGridLines, 'width': s.column_dimensions['A'].width, 'header_fill': s['A1'].fill.fgColor.rgb, 'header_bold': s['A1'].font.bold, 'header_height': s.row_dimensions[1].height, 'row_height': s.row_dimensions[2].height, 'wrap': s['A2'].alignment.wrap_text, 'amount_align': s['E2'].alignment.horizontal, 'stripe': s['A3'].fill.fgColor.rgb, 'status_fill': s['K2'].fill.fgColor.rgb} for s in wb}))
 sys.exit(0)
print(json.dumps({s.title: [[{'value': c.value, 'type': c.data_type} for c in row] for row in s] for s in wb}, ensure_ascii=False))
`, layout ? 'layout' : 'values'], { input: Buffer.from(await blob.arrayBuffer()), encoding: 'utf8' })
  assert.equal(result.status, 0, result.stderr)
  return JSON.parse(result.stdout)
}

test('personal export is a real XLSX preserving original fields, full scope, IDs and decimal text', async () => {
  const blob = await createSellerExcel(group, '测试部门')
  assert.equal(blob.type, recordsExcelMime)
  assert.deepEqual([...new Uint8Array(await blob.arrayBuffer()).slice(0, 4)], [0x50, 0x4b, 0x03, 0x04])
  const workbook = await readWorkbook(blob)
  assert.deepEqual(Object.keys(workbook), ['提价记录', '导出信息'])
  const rows = workbook['提价记录']
  assert.equal(rows.length, 4)
  assert.deepEqual(rows[0].map(c => c.value), ['商品编码（SKU）', '店铺名称／编号', '站点', '平台', '一次毛利金额',
    '毛利币种', '价格币种', '原营销价', '目标营销价', '上传价格', '记录状态', '更新时间（北京时间）', '是否模拟数据'])
  assert.deepEqual(rows[1].map(c => c.value), [item.key.sku, item.key.shop_id, 'PH', 'Tiktok', item.first_margin.amount,
    'CNY', 'PHP', item.decision.current_actual, item.decision.target_actual, item.execution.uploaded_price,
    '待核验', '2026-11-23 09:00:00', '否'])
  for (const index of [0, 1, 4, 7, 8, 9]) assert.equal(rows[1][index].type, 's')
  assert.equal(rows[2][0].value, '=1+1')
  assert.equal(rows[2][0].type, 's', 'formula-looking SKU must stay text')
  assert.equal(rows[2][1].value, '店铺名称')
  assert.deepEqual(rows[2].slice(4, 10).map(c => c.value), [null, null, null, null, null, null])
  assert.equal(rows[2][12].value, '是')
  assert.equal(rows[3][4].value, '0')
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
  assert.match(source, /await downloadSellerExcel\(group, departmentName\.value\)/)
  assert.match(source, /Excel 下载未成功，请重试。/)
  assert.doesNotMatch(source, /下载个人数据|提价记录\.json|JSON\.stringify/)
})

test('log dialog header offers the same Excel download for the active seller', async () => {
  const source = await readFile(new URL('../src/RecordsPage.vue', import.meta.url), 'utf8')
  const header = source.match(/<div class="records-log-title">[\s\S]*?<\/div>/)
  assert.ok(header, 'dialog header must keep the records-log-title wrapper')
  assert.match(header[0], /v-if="activeSeller"/)
  assert.match(header[0], /title="下载Excel" aria-label="下载Excel"/)
  assert.match(header[0], /@click\.stop="downloadSeller\(activeSeller\)"/)
  assert.match(header[0], /records-log-download/)
})


test('export layout keeps headers and identities visible with readable styled rows', async () => {
  const workbook = await readWorkbook(await createSellerExcel(group, '测试部门'), true)
  const sheet = workbook['提价记录']
  assert.equal(sheet.freeze, 'C2')
  assert.equal(sheet.grid, false)
  assert.equal(sheet.width, 32)
  assert.equal(sheet.header_fill, 'FF243B53')
  assert.equal(sheet.header_bold, true)
  assert.equal(sheet.header_height, 34)
  assert.equal(sheet.row_height, 36)
  assert.equal(sheet.wrap, true)
  assert.equal(sheet.amount_align, 'right')
  assert.equal(sheet.stripe, 'FFF0F4F8')
  assert.equal(sheet.status_fill, 'FFFFF4CE')
  assert.equal(workbook['导出信息'].freeze, 'A2')
})

test('export converts explicit time zones across Beijing midnight without guessing missing time zones', async () => {
  const items = ['2026-11-23T17:30:00Z', '2026-11-24T09:30:00+08:00', '2026-11-23 01:00:00', 'invalid', null]
    .map(updated_at => ({ ...item, updated_at }))
  const workbook = await readWorkbook(await createSellerExcel({ ...group, items }, '测试部门'))
  assert.deepEqual(workbook['提价记录'].slice(1).map(row => row[11].value), [
    '2026-11-24 01:30:00', '2026-11-24 09:30:00', '2026-11-23 01:00:00', 'invalid', null,
  ])
})
