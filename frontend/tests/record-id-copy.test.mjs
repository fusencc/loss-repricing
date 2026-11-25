import test from 'node:test'
import assert from 'node:assert/strict'
import { copyRecordId } from '../src/record-id-copy.ts'
import { recordStageLabel } from '../src/records-model.ts'

function copyDocument(result = true) {
  const events = [], written = []
  let area
  const selection = { rangeCount: 1, getRangeAt: () => ({ cloneRange: () => 'saved-range' }),
    removeAllRanges() { events.push('clear-selection') }, addRange(range) { events.push(range) } }
  const container = { appendChild(node) { area = node; events.push('append-in-dialog') } }
  const activeElement = { closest: () => container, focus() { events.push('restore-focus') } }
  const document = { activeElement, body: { appendChild() { assert.fail('dialog copy must stay inside its focus scope') } },
    getSelection: () => selection,
    createElement(tag) {
      assert.equal(tag, 'textarea')
      return { style: {}, setAttribute() {}, focus() { events.push('focus-copy') },
        select() { events.push('select') }, setSelectionRange(start, end) { assert.equal(start, 0); assert.equal(end, this.value.length) },
        remove() { events.push('remove') } }
    },
    execCommand(command) {
      assert.equal(command, 'copy'); written.push(area.value)
      assert.equal(area.readOnly, true)
      if (result instanceof Error) throw result
      return result
    },
  }
  return { document, events, written }
}

test('HTTP page copies visible shop values, raw SKU and long or leading-zero IDs without Clipboard API or prefixes', async () => {
  const { document, written } = copyDocument()
  for (const [value, label] of [['合成测试店铺', '店铺'], ['1912442718471573506', '店铺'], ['YWAC442-RG', 'SKU'], ['1731919633741415702', '商品ID'], ['000123', '商品ID']]) {
    assert.deepEqual(await copyRecordId(value, label, document), { type: 'success', message: `${label}已复制。`, duration: 2000 })
  }
  assert.deepEqual(written, ['合成测试店铺', '1912442718471573506', 'YWAC442-RG', '1731919633741415702', '000123'])
})

test('copy confirms the browser result, cleans temporary text and restores dialog focus and selection', async () => {
  const { document, events } = copyDocument()
  assert.equal((await copyRecordId('SKU-001', 'SKU', document)).type, 'success')
  assert.deepEqual(events, ['append-in-dialog', 'focus-copy', 'select', 'remove', 'restore-focus', 'clear-selection', 'saved-range'])
  for (const failure of [false, new Error('denied')]) {
    const fake = copyDocument(failure)
    assert.deepEqual(await copyRecordId('000123', '商品ID', fake.document),
      { type: 'error', message: '商品ID复制失败，请选中文本手动复制。', duration: 3000 })
    assert.ok(fake.events.includes('remove'))
    assert.ok(fake.events.includes('restore-focus'))
  }
  const fake = copyDocument()
  assert.deepEqual(await copyRecordId('', 'SKU', fake.document),
    { type: 'error', message: 'SKU未记录，无法复制。', duration: 3000 })
  assert.deepEqual(fake.written, [])
})

test('stage labels change only display semantics and preserve reference-priority evidence', () => {
  for (const [label, expected] of [['① 原实际营销价', '原营销价'], ['② 补亏损', '需补亏损'], ['③ 利润校验', '利润校验变动'], ['③ 参考价优先', '参考价优先']]) {
    const stage = { label, price: '12.3456789', key: 'break_even' }
    const saved = structuredClone(stage)
    assert.equal(recordStageLabel(stage), expected)
    assert.deepEqual(stage, saved)
  }
})
