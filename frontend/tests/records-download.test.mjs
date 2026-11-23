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
async function readWorkbook(blob) {
  const python = new URL('../../.venv/bin/python', import.meta.url).pathname
  const result = spawnSync(python, ['-c', `
import io, json, sys
from openpyxl import load_workbook
wb = load_workbook(io.BytesIO(sys.stdin.buffer.read()))
print(json.dumps({s.title: [[{'value': c.value, 'type': c.data_type} for c in row] for row in s] for s in wb}, ensure_ascii=False))
`], { input: Buffer.from(await blob.arrayBuffer()), encoding: 'utf8' })
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
  assert.deepEqual(rows[0].map(c => c.value), ['sku', 'shop', 'site', 'platform', 'first_margin.amount',
    'first_margin.currency', 'currency', 'current_price', 'target_price', 'uploaded_price', 'status', 'updated_at', 'synthetic'])
  assert.deepEqual(rows[1].map(c => c.value), [item.key.sku, item.key.shop_id, 'PH', 'Tiktok', item.first_margin.amount,
    'CNY', 'PHP', item.decision.current_actual, item.decision.target_actual, item.execution.uploaded_price,
    '待核验', item.updated_at, false])
  for (const index of [0, 1, 4, 7, 8, 9]) assert.equal(rows[1][index].type, 's')
  assert.equal(rows[2][0].value, '=1+1')
  assert.equal(rows[2][0].type, 's', 'formula-looking SKU must stay text')
  assert.equal(rows[2][1].value, '店铺名称')
  assert.deepEqual(rows[2].slice(4, 10).map(c => c.value), [null, null, null, null, null, null])
  assert.equal(rows[2][12].value, true)
  assert.equal(rows[3][4].value, '0')
  assert.deepEqual(workbook['导出信息'].map(r => r.map(c => c.value)), [['seller', group.name], ['department', '测试部门']])
})

test('empty export remains a readable workbook with headers and seller metadata', async () => {
  const workbook = await readWorkbook(await createSellerExcel({ ...group, items: [] }, '测试部门'))
  assert.equal(workbook['提价记录'].length, 1)
  assert.equal(workbook['导出信息'][0][1].value, group.name)
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
