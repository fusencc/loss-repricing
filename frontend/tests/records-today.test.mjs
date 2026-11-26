import test from 'node:test'
import assert from 'node:assert/strict'
import { groupBySeller, todayBusinessDateRange, filterRecordBusinessDates, countRecordStatuses } from '../src/records-model.ts'

const item = (id, business_date, status, seller = 'a') => ({
  id, business_date, status, seller: { id: seller, name: seller }, execution: null,
  updated_at: '2026-11-26T12:00:00Z',
})

test('today summaries use Beijing business date and retain historical logs and latest priced record', () => {
  const range = todayBusinessDateRange(new Date('2026-11-25T16:00:00Z'))
  assert.deepEqual(range, { start: '2026-11-26', end: '2026-11-26' })
  const rows = [item('1', '2026-11-26', '已提价'), item('2', '2026-11-26', '待处理'),
    item('3', '2026-11-26', '不调价'), item('4', '2026-11-25', '已提价'),
    item('5', '2026-11-25', '待处理', 'b'), item('6', '2026-11-27', '已提价')]
  const saved = structuredClone(rows)
  const today = filterRecordBusinessDates(rows, range)
  assert.deepEqual(today.map(row => row.id), ['1', '2', '3'])
  assert.deepEqual(countRecordStatuses(today), { '已提价': 1, '待处理': 1, '不调价': 1 })
  const [a, b] = groupBySeller(rows, range)
  assert.deepEqual(a.counts, countRecordStatuses(today))
  assert.equal(a.items.length, 5)
  assert.equal(a.latestPriced, rows[0])
  assert.deepEqual(b.counts, { '已提价': 0, '待处理': 0, '不调价': 0 })
  assert.deepEqual(b.items, [rows[4]])
  assert.deepEqual(rows, saved)
})

test('Beijing midnight changes the summary range, and an old record updated today stays excluded', () => {
  const before = todayBusinessDateRange(new Date('2026-11-25T15:59:59Z'))
  const after = todayBusinessDateRange(new Date('2026-11-25T16:00:00Z'))
  const rows = [item('old', '2026-11-25', '已提价')]
  assert.equal(groupBySeller(rows, before)[0].counts['已提价'], 1)
  assert.equal(groupBySeller(rows, after)[0].counts['已提价'], 0)
  assert.deepEqual(filterRecordBusinessDates(rows, after), [])
})
