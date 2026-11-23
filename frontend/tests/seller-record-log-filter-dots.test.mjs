import test, { after, before } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createServer } from 'vite'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'
import { groupBySeller, recordTone } from '../src/records-model.ts'

let server, Component
before(async () => {
  server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false }, appType: 'custom' })
  Component = (await server.ssrLoadModule('/src/SellerRecordLogs.vue')).default
})
after(async () => { await server?.close() })

test('only the three status filters have decorative tone dots before their unchanged labels and counts', async () => {
  const rows = ['已核验', '待核验', '待处理', '本轮不调'].map((status, index) => ({
    id: `synthetic-dot-${index}`, status,
    seller: { id: 'synthetic-dot-seller', name: '合成测试' },
    key: { sku: '', item_id: '', shop_id: 'synthetic-shop', site: 'PH' },
  }))
  const original = structuredClone(rows)
  const html = await renderToString(createSSRApp(Component, { seller: groupBySeller(rows)[0] }))
  const filter = html.match(/<div class="records-log-states">([\s\S]*?)<\/div>/)?.[1]
  assert.ok(filter)
  const buttons = [...filter.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)]
  assert.equal(buttons.length, 4)
  for (const [index, [state, label, count]] of [
    ['全部', '全部', 4], ['已核验', '已提价', 1], ['待处理', '待处理', 2], ['本轮不调', '本轮不调', 1],
  ].entries()) {
    const [, attributes, content] = buttons[index]
    assert.match(attributes, /type="button"/)
    assert.match(attributes, new RegExp(`aria-pressed="${index === 0}"`))
    assert.match(content, new RegExp(`${label} <span class="records-log-filter-count">${count}</span>$`))
    if (state === '全部') {
      assert.doesNotMatch(content, /<i\b|records-log-filter-dot/)
    } else {
      assert.ok(content.startsWith(`<i class="${recordTone(state)} records-log-filter-dot" aria-hidden="true"></i>${label}`))
    }
  }
  assert.equal((filter.match(/aria-hidden="true"/g) ?? []).length, 3)
  assert.deepEqual(rows, original)
})

test('filter dots reuse existing status palette while native buttons and filtering handlers stay intact', async () => {
  const source = await readFile(new URL('../src/SellerRecordLogs.vue', import.meta.url), 'utf8')
  const css = await readFile(new URL('../src/records.css', import.meta.url), 'utf8')
  assert.match(source, /@click="statusFilter = state"/)
  assert.match(source, /filterRecordItems\(props\.seller\.items, statusFilter\.value, keyword\.value\)/)
  assert.match(source, /:class="recordTone\(state\)" aria-hidden="true"/)
  assert.match(css, /\.records-log-filter-dot\{[^}]*width:5px;height:5px;[^}]*border-radius:50%;background:currentColor;[^}]*color:var\(--r-muted\)/)
  for (const [tone, variable] of [['verified', '--r-mint'], ['attention', '--r-amber']]) {
    assert.match(css, new RegExp(`\\.records-log-filter-dot\\.${tone}\\{color:var\\(${variable}\\)\\}`))
    assert.match(css, new RegExp(`\\.records-status\\.${tone}\\{color:var\\(${variable}\\)`))
    assert.match(css, new RegExp(`\\.records-counts i\\.${tone}\\{color:var\\(${variable}\\)`))
  }
  assert.match(css, /\.records-status\{[^}]*color:var\(--r-muted\)/)
  assert.match(css, /\.records-log-states button:focus-visible\{outline:2px solid var\(--r-mint\)/)
})
