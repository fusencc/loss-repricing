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
  key: { sku: 'SYNTHETIC-001', item_id: '000123', product_id: 'PARENT-999', shop_id: 'SHOP-001', site: 'PH', platform: 'Tiktok' },
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

test('the initial marketing price shows its saved trial profit including zero', async () => {
  const row = item()
  const loss = row.decision.stages.rows.find(stage => stage.key === 'loss')
  assert.doesNotMatch(await render(row), /按初步补亏后的营销价试算/)
  loss.trial_profit = '-1.0400'
  loss.trial_margin_percent = '-9.29'
  assert.match(await render(row), /按初步补亏后的营销价试算，每件利润 -1\.04 PHP，利润率 -9\.29%/)
  loss.trial_profit = '0.0000'
  loss.trial_margin_percent = '0.00'
  assert.match(await render(row), /每件利润 0 PHP，利润率 0%/)
})

test('dialog title shows evidence dates without total or hit counts, while filter buttons show full seller counts including zero', async () => {
  const { readFile } = await import('node:fs/promises')
  const page = await readFile(new URL('../src/RecordsPage.vue', import.meta.url), 'utf8')
  const source = await readFile(new URL('../src/SellerRecordLogs.vue', import.meta.url), 'utf8')
  assert.match(page, /#header="\{ titleId, titleClass \}"[\s\S]*<h2 :id="titleId"[^>]*>\{\{ logTitle \}\}<\/h2>\s*<p class="records-log-date">\{\{ logDate \}\}<\/p>/)
  assert.doesNotMatch(page + source, /logHitCount|logTotal|records-log-count|hit-count|hitCount|defineEmits/)
  assert.match(source, /seller\.items\.length : recordStatusCount\(seller\.counts, state\)/)
  const seller = groupBySeller([item(), { ...item(), id: 'second', status: '待处理' }, { ...item(), id: 'third', status: '已核验' }])[0]
  const saved = structuredClone(seller)
  const html = await renderToString(createSSRApp(Component, { seller }))
  for (const [name, count] of [['全部', 3], ['已提价', 1], ['待处理', 2], ['本轮不调', 0]]) {
    assert.ok(html.includes(`${name} <span class="records-log-filter-count">${count}</span>`))
  }
  assert.deepEqual(seller, saved)
})

test('compact log retains identity, status, three horizontal stage fields and upload evidence without formula text', async () => {
  const row = item()
  const original = structuredClone(row)
  const html = await render(row)
  assert.match(html, /SYNTHETIC-001/)
  assert.match(html, /<p>合成测试店铺 \/ PH<\/p>/)
  assert.doesNotMatch(html, /PH \/ Tiktok/)
  assert.equal(row.key.platform, 'Tiktok')
  assert.match(html, /class="attention records-status"><i><\/i>待处理/)
  assert.equal((html.match(/data-stage=/g) ?? []).length, 3)
  assert.equal((html.match(/class="records-stage-field"/g) ?? []).length, 5)
  assert.doesNotMatch(html, /<table|<thead|<tbody|<tr|scope="col"/)
  assert.match(html, /<dt>原实际营销价<\/dt><dd><strong>10 PHP<\/strong>/)
  assert.match(html, /<dt>补毛利亏<\/dt><dd><strong>12\.35 PHP<\/strong>/)
  assert.match(html, /<dt>利润校验变动<\/dt><dd><strong>14\.57 PHP<\/strong>/)
  assert.match(html, /<header class="records-log-heading">.*商品ID：000123.*<\/header>/s)
  assert.doesNotMatch(html, /<header class="records-log-heading">(?:(?!<\/header>).)*修正一次毛利/s)
  assert.match(html, /data-field="first-margin"><dt>修正一次毛利<\/dt><dd><strong>-2\.35 CNY<\/strong>/)
  assert.equal((html.match(/<dt>修正一次毛利<\/dt>/g) ?? []).length, 1)
  assert.equal((html.match(/上传含税价/g) ?? []).length, 1)
  assert.match(html, /class="records-stage-field" data-field="uploaded-price"><dt>上传含税价<\/dt><dd><strong>18\.24 PHP<\/strong><\/dd><\/div><\/dl>/)
  assert.doesNotMatch(html, /折合人民币|1\.75 元|建议上传价|18\.23 PHP/)
  for (const text of ['10 PHP', '12.35 PHP', '14.57 PHP']) {
    assert.ok(html.includes(text), `missing saved display: ${text}`)
  }
  assert.match(html, /<dl class="records-log-stages" aria-label="毛利与价格明细">/)
  assert.doesNotMatch(html, /records-stage-change|比上一环节增加|涨幅|\+0\.27 元|\+18%/)
  const { readFile } = await import('node:fs/promises')
  const source = await readFile(new URL('../src/SellerRecordLogs.vue', import.meta.url), 'utf8')
  const css = await readFile(new URL('../src/records.css', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /stageChangeLabel|stagePercentLabel|isIncrease|records-stage-change/)
  assert.doesNotMatch(css, /records-stage-change/)
  assert.doesNotMatch(html, /records-stage-table|可横向滚动/)
  assert.doesNotMatch(html, /records-stage-formula|公式：|P＋Δ|在线利润率导致的变动|999\.99/)
  assert.deepEqual(row, original)
})

test('upload is a peer price field and all execution statuses occupy a separate rightmost top-aligned column', async () => {
  for (const [status, tone, label] of [
    ['已核验', 'verified', '已提价'], ['待核验', 'attention', '待处理'],
    ['待处理', 'attention', '待处理'], ['本轮不调', 'neutral', '本轮不调'],
  ]) {
    const row = item()
    row.status = status
    const original = structuredClone(row)
    const html = await render(row)
    assert.match(html, new RegExp(`<div class="records-stage-field" data-field="uploaded-price"><dt>上传含税价</dt><dd><strong>18\\.24 PHP</strong></dd></div></dl>(?:<!--.*?-->)*<div class="records-log-state" aria-label="执行状态"><span class="${tone} records-status"><i></i>${label}</span><p class="records-log-reason">${status === '已核验' ? '商品符合调价' : '原因未记录'}</p></div>`))
    assert.equal((html.match(/class="records-log-state"/g) ?? []).length, 1)
    assert.doesNotMatch(html, /<dl[^>]*>(?:(?!<\/dl>).)*records-log-state/s)
    assert.doesNotMatch(html, /<header class="records-log-heading">(?:(?!<\/header>).)*records-log-state/s)
    assert.deepEqual(row, original)
  }
  const { readFile } = await import('node:fs/promises')
  const css = await readFile(new URL('../src/records.css', import.meta.url), 'utf8')
  assert.match(css, /\.records-log\{display:grid;grid-template-columns:minmax\(min-content,1\.2fr\) minmax\(0,4fr\) minmax\(120px,\.85fr\);align-items:start;/)
  assert.match(css, /\.records-log-state\{grid-column:3;grid-row:1;align-self:start;justify-self:stretch;display:flex;flex-direction:column;align-items:center;gap:7px;min-width:0\}/)
  assert.match(css, /\.records-log-stages\{display:grid;grid-template-columns:repeat\(5,minmax\(0,1fr\)\);align-items:start;/)
  assert.match(css, /@media\(max-width:900px\)\{[\s\S]*?\.records-log-state\{grid-column:2;grid-row:1\}[\s\S]*?\.records-log-stages\{grid-column:1\/-1;grid-row:2\}/)
  assert.match(css, /@media\(max-width:680px\)\{\s*\.records-log\{padding:12px 0;gap:10px\}\s*\.records-log-stages\{grid-template-columns:repeat\(2,minmax\(0,1fr\)\)\}/)
  assert.doesNotMatch(css, /records-log-uploads|records-log-prices/)
  assert.doesNotMatch(css, /\.records-log-state\{[^}]*(?:position:|transform:|margin:|top:)/)
})

test('long saved upload amounts remain a single peer field with a separate status', async () => {
  const row = item()
  row.execution.uploaded_price = '12345678901234567890.236'
  const original = structuredClone(row)
  const html = await render(row)
  assert.match(html, /data-field="uploaded-price"><dt>上传含税价<\/dt><dd><strong>12345678901234567890\.24 PHP<\/strong><\/dd><\/div><\/dl>/)
  assert.match(html, /<div class="records-log-state" aria-label="执行状态">/)
  assert.deepEqual(row, original)
  const { readFile } = await import('node:fs/promises')
  const css = await readFile(new URL('../src/records.css', import.meta.url), 'utf8')
  assert.match(css, /\.records-stage-field\{min-width:0\}/)
  assert.match(css, /\.records-stage-field dd\{margin:0;overflow-wrap:anywhere\}/)
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
  assert.match(html, /<dt>修正一次毛利<\/dt><dd><strong>未记录<\/strong>/)
  assert.match(html, /<dt>上传含税价<\/dt><dd><strong>未记录<\/strong><\/dd>/)
  assert.doesNotMatch(html, /18\.23 PHP|987\.65|999\.11/)
})

test('missing concrete item IDs stay optional and never use parent IDs or SKU as replacements', async () => {
  for (const missing of [undefined, null, '']) {
    const row = item()
    row.key.item_id = missing
    const html = await render(row)
    assert.match(html, /商品ID：未记录/)
    assert.doesNotMatch(html, /商品ID：(PARENT-999|SYNTHETIC-001)|子SKU：/)
    assert.match(html, /data-stage="original"/)
    assert.match(html, /<dt>上传含税价<\/dt><dd><strong>18\.24 PHP<\/strong><\/dd>/)
  }
})

test('item ID stays a single text-only copy target while SKU keeps its icon and phone identity has its own row', async () => {
  const row = item()
  row.key.item_id = '1731919633741415702'
  const html = await render(row)
  const buttons = [...html.matchAll(/<button[^>]*class="records-id-copy-button"[\s\S]*?<\/button>/g)].map(match => match[0])
  assert.equal(buttons.length, 2)
  assert.match(buttons[0], /复制SKU：SYNTHETIC-001[\s\S]*<svg/)
  assert.match(buttons[1], /复制商品ID：1731919633741415702/)
  assert.match(buttons[1], /title="点击复制商品ID"/)
  assert.match(buttons[1], /<span>商品ID：1731919633741415702<\/span>/)
  assert.doesNotMatch(buttons[1], /<svg|<br|复制<\/span>/)
  const { readFile } = await import('node:fs/promises')
  const css = await readFile(new URL('../src/records.css', import.meta.url), 'utf8')
  const source = await readFile(new URL('../src/RecordIdCopy.vue', import.meta.url), 'utf8')
  assert.match(source, /showIcon\?: boolean/)
  assert.match(source, /showIcon: true/)
  assert.match(source, /<CopyDocument v-if="showIcon"/)
  assert.match(css, /\.records-id-copy-text-only \.records-id-copy-button\{white-space:nowrap\}/)
  assert.match(css, /@media\(max-width:480px\)\{[\s\S]*?\.records-log-heading\{grid-column:1;grid-row:1\}[\s\S]*?\.records-log-state\{grid-column:1;grid-row:2;align-items:flex-start\}[\s\S]*?\.records-log-stages\{grid-column:1;grid-row:3\}/)
  assert.doesNotMatch(css, /text-overflow:ellipsis/)
})

test('identity copy buttons expose distinct accessible names and missing IDs remain plain text', async () => {
  const html = await render(item())
  assert.match(html, /<h3>.*<button type="button" class="records-id-copy-button" aria-label="复制SKU：SYNTHETIC-001" title="点击复制SKU"/s)
  assert.match(html, /aria-label="复制商品ID：000123" title="点击复制商品ID"/)
  assert.match(html, /<span>商品ID：000123<\/span>/)
  assert.doesNotMatch(html, /records-id-copy-feedback|aria-live|已复制。|复制失败/)
  const { readFile } = await import('node:fs/promises')
  const source = await readFile(new URL('../src/RecordIdCopy.vue', import.meta.url), 'utf8')
  const css = await readFile(new URL('../src/records.css', import.meta.url), 'utf8')
  assert.match(source, /import \{ ElMessage \} from 'element-plus'/)
  assert.match(source, /ElMessage\(await copyRecordId\(props\.value, props\.label, navigator\.clipboard\)\)/)
  assert.match(source, /finally\s*\{\s*copying\.value = false/)
  assert.doesNotMatch(source, /feedback|setTimeout|setInterval|aria-live/)
  assert.doesNotMatch(css, /records-id-copy-feedback/)
  const row = item()
  row.key.sku = ''
  row.key.item_id = ''
  const missing = await render(row)
  assert.match(missing, /SKU 未记录/)
  assert.match(missing, /商品ID：未记录/)
  assert.doesNotMatch(missing, /records-id-copy-button/)
})

test('saved reasons appear below the badge for pending and skipped records without using earlier attempts', async () => {
  for (const status of ['待处理', '待核验', '本轮不调']) {
    const row = item()
    row.status = status
    row.reason = '近72小时内有调价记录\n人工保护；来源待核实'
    row.process = [{ reason: '旧尝试原因，不应替代当前原因' }]
    const original = structuredClone(row)
    const html = await render(row)
    assert.match(html, /<p class="records-log-reason">近72小时内有调价记录\n人工保护；来源待核实<\/p>/)
    assert.doesNotMatch(html, /旧尝试原因|商品符合调价/)
    assert.deepEqual(row, original)
  }
})

test('missing saved reasons are explicit and never inferred from other evidence', async () => {
  for (const status of ['待处理', '待核验', '本轮不调']) {
    for (const reason of [undefined, null, '', '   ']) {
      const row = item()
      Object.assign(row, { status, reason, seven_days_sales: 22 })
      const html = await render(row)
      assert.match(html, /<p class="records-log-reason">原因未记录<\/p>/)
      assert.doesNotMatch(html, /三天调价保护|7天销量|商品符合调价/)
    }
  }
})

test('verified records use the requested fixed explanation and historical reasons stay unchanged', async () => {
  const row = item()
  row.status = '已核验'
  row.reason = '保存的旧执行文案'
  const html = await render(row)
  assert.match(html, /<p class="records-log-reason">商品符合调价<\/p>/)
  assert.doesNotMatch(html, /保存的旧执行文案/)
  assert.equal(row.reason, '保存的旧执行文案')
})

test('saved historical threshold reasons are displayed as text, escaped, and never re-evaluated', async () => {
  const row = item()
  row.status = '本轮不调'
  row.reason = '历史原因：7天销量>21；<script>不执行</script>'
  const html = await render(row)
  assert.match(html, /历史原因：7天销量&gt;21；&lt;script&gt;不执行&lt;\/script&gt;/)
  assert.doesNotMatch(html, /<script>/)
  const { readFile } = await import('node:fs/promises')
  const css = await readFile(new URL('../src/records.css', import.meta.url), 'utf8')
  assert.match(css, /\.records-log-state \.records-log-reason\{[^}]*white-space:pre-wrap;overflow-wrap:anywhere/)
  assert.match(css, /grid-template-columns:minmax\(min-content,1fr\) minmax\(0,\.8fr\)/)
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
