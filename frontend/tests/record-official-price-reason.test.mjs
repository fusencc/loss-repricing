import test from 'node:test'
import assert from 'node:assert/strict'
import { recordStateReason } from '../src/records-model.ts'

test('skipped official-price reasons show the count saved in either known wording', () => {
  for (const [reason, expected] of [
    ['近72小时存在调价日志，不调价（官方记录 1 条）', '近3天存在1条调价'],
    ['近72小时存在调价日志，不调价（官方记录 12 条）', '近3天存在12条调价'],
    ['近三天存在23条调价日志', '近3天存在23条调价'],
  ]) {
    const item = { status: '不调价', reason }
    assert.equal(recordStateReason(item), expected)
    assert.equal(item.reason, reason)
  }
})

test('skipped reasons without an exact counted official-price wording are unchanged', () => {
  for (const reason of [
    '近72小时存在调价日志，不调价',
    '提交前查到近72小时调价日志，不调价',
    '距离上次确认生效不足 72 小时',
    '命中人工保护名单（官方记录 1 条）',
    '近72小时存在调价日志，不调价（官方记录 0 条）',
    '近72小时存在调价日志，不调价（官方记录 1 条）；存在其他保护原因',
    '另有提示：近三天存在2条调价日志',
  ]) {
    assert.equal(recordStateReason({ status: '不调价', reason }), reason)
  }
})
