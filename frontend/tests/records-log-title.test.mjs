import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { filterRecordBusinessDates, todayBusinessDateRange } from '../src/records-model.ts'

test('default range changes at Beijing midnight, not UTC midnight', () => {
  for (const [instant, expected] of [
    ['2026-11-24T15:59:59Z', '2026-11-24'],
    ['2026-11-24T16:00:00Z', '2026-11-25'],
    ['2026-12-31T16:00:00Z', '2027-01-01'],
  ]) {
    assert.deepEqual(todayBusinessDateRange(new Date(instant)), { start: expected, end: expected })
  }
})

test('date filtering includes both endpoints and uses business dates, not later updates', () => {
  const row = (business_date, updated_at = '2026-11-27T01:00:00Z') => ({ business_date, updated_at })
  const rows = [row('2026-11-26'), row('2026-11-25'), row('2026-11-23', '2026-11-25T01:00:00Z'), row('2026-11-24')]
  const saved = structuredClone(rows)
  assert.deepEqual(filterRecordBusinessDates(rows, { start: '2026-11-24', end: '2026-11-25' }), [rows[1], rows[3]])
  assert.deepEqual(filterRecordBusinessDates(rows, { start: '2026-11-24', end: '2026-11-24' }), [rows[3]])
  assert.deepEqual(filterRecordBusinessDates(rows, { start: '2026-11-27', end: '2026-11-27' }), [])
  assert.deepEqual(rows, saved)
})

test('date controls and download are together and leave extra space beside the close button', async () => {
  const source = await readFile(new URL('../src/RecordsPage.vue', import.meta.url), 'utf8')
  const css = await readFile(new URL('../src/records.css', import.meta.url), 'utf8')
  assert.match(source, /<h2[^>]*>\{\{ logTitle \}\}<\/h2>\s*<div[^>]*records-log-actions/)
  assert.doesNotMatch(source, /records-log-today|<input type="date"/)
  assert.doesNotMatch(css, /records-log-today/)
  assert.match(source, /function openSeller\([\s\S]*?logRange\.value = todayBusinessDateRange\(\)/)
  for (const [boundary, label] of [['start', '开始日期'], ['end', '结束日期']]) {
    const picker = source.match(new RegExp(`<el-date-picker\\b[^>]*aria-label="${label}"[\\s\\S]*?/>`))?.[0] ?? ''
    assert.match(picker, /type="date"/)
    assert.match(picker, /value-format="YYYY-MM-DD"/)
    assert.match(picker, /popper-class="records-log-calendar"/)
    assert.match(picker, /:clearable="false"/)
    assert.ok(picker.includes(`:model-value="logRange.${boundary}"`))
    assert.ok(picker.includes(`@update:model-value="changeLogDate('${boundary}', $event)"`))
  }
  assert.match(source, /filterRecordBusinessDates\(activeSeller\.value\?\.items \?\? \[\], logRange\.value\)/)
  assert.match(source, /downloadSeller\(\{ name: activeSeller\.name, items: logItems \}, logRange\)/)
  assert.match(css, /\.records-log-calendar\.el-popper\{[^}]*--el-bg-color-overlay:rgba\(/)
  assert.match(css, /\.records-log-title\{[^}]*display:flex;[^}]*padding-right:16px/)
  assert.match(css, /@media\(max-width:720px\)\{\.records-log-title\{flex-direction:column/)
})
