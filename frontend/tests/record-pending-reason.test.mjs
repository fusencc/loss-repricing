import test from 'node:test'
import assert from 'node:assert/strict'
import { recordStateReason, recordStatusLabel } from '../src/records-model.ts'

test('in-flight and uncertain uploads never instruct the seller to reupload', () => {
  for (const state of ['unknown', 'submitting', 'mismatch']) {
    const item = { status: '待核验', execution: { state, submit_status: 'accepted' } }
    assert.equal(recordStatusLabel(item), '待处理')
    assert.equal(recordStateReason(item), '上传结果待确认，请勿重复上传')
  }
  assert.equal(recordStateReason({status: '待核验', phase: 'blocked'}), '上传结果待确认，请勿重复上传')
})

test('accepted roster projections include old readback mismatches but never explicit rejection', () => {
  for (const state of ['accepted', 'uploaded', 'verified', 'mismatch']) {
    const item = {status: '已提价', execution: { state, submit_status: 'accepted' }}
    assert.equal(recordStatusLabel(item), '已提价')
    assert.equal(recordStateReason(item), '提价上传完成')
  }
  for (const execution of [{state: 'failed', submit_status: 'accepted'},
    {state: 'accepted', row_status: 'rejected'}, {state: 'uploaded', submit_status: 'rejected'}]) {
    const item = {status: '已提价', execution}
    assert.equal(recordStatusLabel(item), '待处理')
    assert.equal(recordStateReason(item), '处理异常需要人工提价')
  }
  assert.equal(recordStateReason({status: '不调价', reason: '近72小时内有调价记录'}), '近72小时内有调价记录')
  assert.equal(recordStateReason({status: '不调价', reason: ''}), '原因未记录')
})
