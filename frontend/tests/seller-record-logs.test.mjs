import test, { after, before } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { groupBySeller } from '../src/records-model.ts'

let server, Component
before(async () => {
  server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false }, appType: 'custom' })
  Component = (await server.ssrLoadModule('/src/SellerRecordLogs.vue')).default
})
after(async () => { await server?.close() })

const item = () => ({
  id: 'compact-log', seller: { id: 'synthetic-seller', name: '测试销售员' }, status: '待核验',
  key: { sku: 'SYNTHETIC-001', shop_id: 'SHOP-001', site: 'PH', platform: 'Tiktok' },
  shop_name: '合成测试店铺', first_margin: { amount: '-2.346', currency: 'CNY' },
  decision: { current_actual: '10', target_actual: '14.567', upload_price: '18.234',
    price_change: '4.567', trace: { station_currency: 'PHP' },
    stages: { currency: 'PHP', rows: [
      { key: 'original', label: '① 原实际营销价', price: '10', cny: '1.2', change_cny: null, change_percent: null },
      { key: 'loss', label: '② 补亏损', price: '12.345', cny: '1.4814', change_cny: '0.2814', change_percent: '23.45' },
      { key: 'break_even', label: '③ 利润校验', price: '14.567', cny: '1.74804', change_cny: '0.26664', change_percent: '18', profit_change_cny: '999.99' },
    ] } },
  execution: { uploaded_price: '18.236' },
})
const render = async row => renderToString(createSSRApp(Component, { seller: groupBySeller([row])[0] }))

test('the dialog count follows its accessible title and receives the log filter count', async () => {
  const { readFile } = await import('node:fs/promises')
  const page = await readFile(new URL('../src/RecordsPage.vue', import.meta.url), 'utf8')
  assert.match(page, /#header="\{ titleId, titleClass \}"[\s\S]*<h2 :id="titleId"[^>]*>\{\{ logTitle \}\}<\/h2>\s*<span class="records-log-count" role="status" aria-atomic="true">/)
  assert.match(page, /共 \{\{ logTotal \}\} 条记录.*当前筛选命中 \{\{ logHitCount \}\} 条/)
  assert.match(page, /@hit-count="logHitCount = \$event"/)
  const counts = []
  const seller = groupBySeller([item()])[0]
  await renderToString(createSSRApp(Component, { seller, onHitCount: count => counts.push(count) }))
  assert.deepEqual(counts, [1])
})

test('compact log retains identity, status, three horizontal stage fields and upload evidence without formula text', async () => {
  const row = item()
  const original = structuredClone(row)
  const html = await render(row)
  assert.match(html, /SYNTHETIC-001/)
  assert.match(html, /合成测试店铺 \/ PH \/ Tiktok/)
  assert.match(html, /class="attention records-status"><i><\/i>待处理/)
  assert.equal((html.match(/data-stage=/g) ?? []).length, 3)
  assert.equal((html.match(/class="records-stage-field"/g) ?? []).length, 3)
  assert.doesNotMatch(html, /<table|<thead|<tbody|<tr|scope="col"/)
  assert.match(html, /<dt>① 原实际营销价<\/dt><dd><strong>10 PHP<\/strong>/)
  assert.match(html, /<dt>② 补亏损<\/dt><dd><strong>12\.35 PHP<\/strong>/)
  assert.match(html, /<dt>③ 利润校验<\/dt><dd><strong>14\.57 PHP<\/strong>/)
  assert.match(html, /<header class="records-log-heading">.*原修正一次毛利：-2\.35 CNY.*<\/header>/s)
  assert.match(html, /class="records-log-prices records-log-uploads"><div><dt>上传含税价<\/dt><dd>18\.24 PHP<\/dd><\/div><\/dl>/)
  assert.doesNotMatch(html, /折合人民币|1\.75 元|建议上传价|18\.23 PHP/)
  for (const text of ['10 PHP', '12.35 PHP', '14.57 PHP', '+0.27 元', '+18%']) {
    assert.ok(html.includes(text), `missing saved display: ${text}`)
  }
  assert.match(html, /<dl class="records-log-stages" aria-label="三阶段价格明细">/)
  assert.match(html, /class="records-stage-change"/)
  assert.match(html, /比上一环节增加/)
  assert.match(html, /涨幅/)
  assert.doesNotMatch(html, /records-stage-table|可横向滚动/)
  assert.doesNotMatch(html, /records-stage-formula|公式：|P＋Δ|在线利润率导致的变动|999\.99/)
  assert.deepEqual(row, original)
})

test('all log statuses use the independent right-side area without changing their labels', async () => {
  for (const [status, tone, label] of [
    ['已核验', 'verified', '已提价'], ['待核验', 'attention', '待处理'],
    ['待处理', 'attention', '待处理'], ['本轮不调', 'neutral', '本轮不调'],
  ]) {
    const row = item()
    row.status = status
    const html = await render(row)
    assert.match(html, new RegExp(`</header>\\s*<div class="records-log-state"><span class="${tone} records-status"><i></i>${label}</span></div>`))
    assert.doesNotMatch(html, /<header class="records-log-heading">(?:(?!<\/header>).)*records-log-state/s)
  }
  const { readFile } = await import('node:fs/promises')
  const css = await readFile(new URL('../src/records.css', import.meta.url), 'utf8')
  assert.match(css, /\.records-log-state\{grid-column:4;grid-row:1;justify-self:end\}/)
  assert.match(css, /@media\(max-width:900px\)\{[\s\S]*\.records-log-heading\{grid-column:1;grid-row:1\}\s*\.records-log-state\{grid-column:2\}/)
  assert.match(css, /\.records-log\{grid-template-columns:minmax\(0,1fr\) auto;padding:12px/)
  assert.match(css, /\.records-log-stages,\.records-log>\.records-log-prices\{grid-column:1\/-1\}/)
})

test('records without stage evidence keep their existing saved two-price display', async () => {
  const row = item()
  row.decision.stages = null
  const html = await render(row)
  for (const text of ['调整前实际价', '10 PHP', '价格变动', '+4.57 PHP', '目标实际价', '14.57 PHP', '上传含税价']) {
    assert.ok(html.includes(text), `missing historical display: ${text}`)
  }
  assert.doesNotMatch(html, /records-stage-table|data-stage=|公式：/)
})

test('missing upload and ERP margin evidence are not replaced by other saved values', async () => {
  const row = item()
  row.first_margin = null
  row.corrected_first_margin = { amount: '-999.11', currency: null }
  row.execution = { uploaded_price: null, actual_price: '987.65' }
  const html = await render(row)
  assert.match(html, /原修正一次毛利：未记录/)
  assert.match(html, /<dt>上传含税价<\/dt><dd>未记录<\/dd>/)
  assert.doesNotMatch(html, /18\.23 PHP|987\.65|999\.11/)
})

test('optional reference evidence and price-cap explanation remain accessible', async () => {
  const row = item()
  row.decision.sku_price_reference = { marketing_price: '14.567', currency: 'PHP', samples: [] }
  row.decision.price_limit = { applied: true, upper_actual: '14.567', reduction_amount: '0.2' }
  const html = await render(row)
  assert.match(html, /同 SKU Top3 营销参考价/)
  assert.match(html, /目标营销价上限/)
  assert.match(html, /参考价优先/)
  assert.doesNotMatch(html, /records-stage-formula|公式：/)
})
