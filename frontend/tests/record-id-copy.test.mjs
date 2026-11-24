import test from 'node:test'
import assert from 'node:assert/strict'
import { copyRecordId } from '../src/record-id-copy.ts'
import { recordStageLabel } from '../src/records-model.ts'

test('copy preserves raw SKU and long or leading-zero IDs without a display prefix', async () => {
  const written = []
  const clipboard = { async writeText(value) { written.push(value) } }
  for (const [value, label] of [['YWAC442-RG', 'SKU'], ['1731919633741415702', '商品ID'], ['000123', '商品ID']]) {
    assert.deepEqual(await copyRecordId(value, label, clipboard), { type: 'success', message: `${label}已复制。`, duration: 2000 })
  }
  assert.deepEqual(written, ['YWAC442-RG', '1731919633741415702', '000123'])
})

test('copy confirms completion before reporting success and exposes rejection or missing capability', async () => {
  let resolveWrite
  const pending = copyRecordId('000123', '商品ID', { writeText: () => new Promise(resolve => { resolveWrite = resolve }) })
  let finished = false
  pending.then(() => { finished = true })
  await Promise.resolve()
  assert.equal(finished, false)
  resolveWrite()
  assert.deepEqual(await pending, { type: 'success', message: '商品ID已复制。', duration: 2000 })
  assert.deepEqual(await copyRecordId('000123', '商品ID', { async writeText() { throw new Error('denied') } }),
    { type: 'error', message: '商品ID复制失败，请选中文本手动复制。', duration: 3000 })
  assert.deepEqual(await copyRecordId('SKU', 'SKU', undefined),
    { type: 'error', message: 'SKU复制失败，请选中文本手动复制。', duration: 3000 })
  assert.deepEqual(await copyRecordId('', 'SKU', { writeText() { assert.fail('missing IDs cannot be copied') } }),
    { type: 'error', message: 'SKU未记录，无法复制。', duration: 3000 })
})

test('stage labels change only display semantics and preserve reference-priority evidence', () => {
  for (const [label, expected] of [['① 原实际营销价', '原营销价'], ['② 补亏损', '需补亏损'], ['③ 利润校验', '利润校验变动'], ['③ 参考价优先', '参考价优先']]) {
    const stage = { label, price: '12.3456789', key: 'break_even' }
    const saved = structuredClone(stage)
    assert.equal(recordStageLabel(stage), expected)
    assert.deepEqual(stage, saved)
  }
})
