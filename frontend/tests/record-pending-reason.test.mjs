import test from 'node:test'
import assert from 'node:assert/strict'
import { recordStateReason, recordStatusLabel } from '../src/records-model.ts'

test('every pending display state gives salespeople the exact manual pricing instruction', () => {
  for (const status of ['待处理', '待核验', '已核验']) {
    for (const state of [null, 'unknown', 'submitting', 'mismatch', 'failed']) {
      for (const reason of [undefined, null, '', '   ', '提交前刷新后候选已消失', '本站点与仓配组合缺少已验证的单品上传和读证据', '提交时未取得完整的销售与平台价格', '核验详情'.repeat(500)]) {
        const item = { status, reason, phase: 'proposed', execution: state ? { state, submit_status: 'accepted' } : null }
        const saved = structuredClone(item)
        assert.equal(recordStatusLabel(item), '待处理')
        assert.equal(recordStateReason(item), '处理异常需要人工提价')
        assert.deepEqual(item, saved)
      }
    }
  }
})

test('successful upload and skipped reasons retain their existing display rules', () => {
  for (const state of ['accepted', 'verified']) {
    assert.equal(recordStateReason({status: '待核验', reason: '旧原因', execution: {state}}), '提价上传完成')
  }
  assert.equal(recordStateReason({status: '不调价', reason: '近72小时内有调价记录'}), '近72小时内有调价记录')
  assert.equal(recordStateReason({status: '不调价', reason: ''}), '原因未记录')
})
