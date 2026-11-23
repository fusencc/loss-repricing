import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { recordDateLabel } from '../src/records-model.ts'

const row = (overrides = {}) => ({ status: '已核验', updated_at: '2026-11-23T04:00:00Z',
  execution: null, ...overrides })
const effective = value => row({ execution: { effective_at: value, effective_time_source: 'effective_at',
  confirmed_at: '2026-11-24T04:00:00Z' } })

test('title dates use saved effective evidence and Beijing calendar days, sorted into a range', () => {
  assert.equal(recordDateLabel([effective('2026-11-23T17:00:00Z'), effective('2026-11-21T17:00:00Z'),
    effective('2026-11-21T17:00:00Z')]), '提价生效日期：2026/11/22 至 2026/11/24（北京时间）')
  assert.equal(recordDateLabel([effective('2026-11-23T00:30:00+08:00')]), '提价生效日期：2026/11/23（北京时间）')
})

test('first confirmation and updates are explicitly different from real effective dates', () => {
  const confirmation = row({ execution: { effective_at: '2026-11-22T17:00:00Z',
    effective_time_source: 'confirmed_at', confirmed_at: '2026-11-22T17:00:00Z' } })
  assert.equal(recordDateLabel([confirmation]), '提价核验日期：2026/11/23（北京时间）')
  assert.equal(recordDateLabel([row({ status: '待核验', execution: confirmation.execution })]),
    '记录更新日期：2026/11/23（北京时间）')
  assert.equal(recordDateLabel([row({ execution: { effective_at: '2026-11-20T00:00:00Z' } })]),
    '记录更新日期：2026/11/23（北京时间）')
  assert.equal(recordDateLabel([row({ status: '本轮不调' })]), '记录更新日期：2026/11/23（北京时间）')
  assert.equal(recordDateLabel([effective('2026-11-21T01:00:00Z'), confirmation, row()]),
    '提价生效日期：2026/11/21；提价核验日期：2026/11/23；记录更新日期：2026/11/23（北京时间）')
})

test('missing, invalid and timezone-less timestamps never become today or business dates', () => {
  assert.equal(recordDateLabel([]), '提价日期未记录')
  for (const updated_at of [undefined, '', 'invalid', '2026-11-23T10:00:00', '2026-11-23']) {
    assert.equal(recordDateLabel([row({ updated_at, business_date: '2026-11-23' })]), '提价日期未记录')
  }
  assert.equal(recordDateLabel([row({ updated_at: null }), effective('2026-11-22T00:00:00Z')]),
    '提价生效日期：2026/11/22；提价日期未记录（北京时间）')
  assert.equal(recordDateLabel([effective('invalid')]), '提价核验日期：2026/11/24（北京时间）')
})

test('title, date and Excel follow natural wrapping flex order without consuming close-button space', async () => {
  const source = await readFile(new URL('../src/RecordsPage.vue', import.meta.url), 'utf8')
  const css = await readFile(new URL('../src/records.css', import.meta.url), 'utf8')
  assert.match(source, /<div class="records-log-title">\s*<h2[^>]*>\{\{ logTitle \}\}<\/h2>\s*<p class="records-log-date">\{\{ logDate \}\}<\/p>\s*<button[^>]*records-log-download/)
  assert.doesNotMatch(source, /logHitCount|logTotal|records-log-count|hit-count|records-log-title-text/)
  assert.match(css, /\.records-log-title\{[^}]*display:flex;[^}]*flex-wrap:wrap/)
  const downloadRule = css.match(/\.records-log-title \.records-log-download\{([^}]*)\}/)[1]
  assert.doesNotMatch(downloadRule, /margin-left:auto|position:absolute|flex:1/)
  assert.doesNotMatch(css, /\.records-log-title-text/)
  assert.match(css, /\.records-dialog \.el-dialog__header\{padding:25px 64px 12px 28px/)
})
