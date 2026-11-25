import test from 'node:test'
import assert from 'node:assert/strict'
import { recordStateReason } from '../src/records-model.ts'

const row = (state, reason = '具体原因', extra = {}) => ({ status: '待核验', reason, execution: state ? { state, ...extra } : null })
test('pending descriptions use only saved evidence and retain details', () => {
  for (const [input, expected] of [
    [{...row(null), phase: 'proposed'}, '待提交；具体原因'],
    [row('unknown'), '提交结果未知，仅核验、不自动重传；具体原因'],
    [row('submitting'), '提交中或结果未确认，仅核验、不自动重传；具体原因'],
    [row('accepted'), '上传已受理，待回读核验；具体原因'],
    [row('mismatch', '实际售价与目标超出误差范围'), '回读核验未通过；实际售价与目标超出误差范围'],
    [row('failed', '行错误', {row_status:'rejected'}), '逐行处理失败；行错误'],
    [row('failed', '接口拒绝', {submit_status:'rejected'}), '提交被拒绝；接口拒绝'],
    [row('failed', '授权关闭', {submit_status:'not_submitted'}), '未提交；授权关闭'],
    [row(null, '缺少汇率'), '缺少汇率'],
    [row('failed', null), '原因未记录'],
  ]) assert.equal(recordStateReason(input), expected)
})
test('empty and long reasons and other records never substitute current reason', () => {
  const long = '核验详情'.repeat(500)
  assert.equal(recordStateReason(row('accepted', long)), `上传已受理，待回读核验；${long}`)
  assert.equal(recordStateReason({...row('unknown', ''), process:[{reason:'其他尝试'}]}), '提交结果未知，仅核验、不自动重传；原因未记录')
  assert.ok(recordStateReason(row('unknown', '上传已受理，等待实际价格回读')).includes('原记录说明（可能为默认文案）'))
  assert.equal(recordStateReason({...row('accepted', '保护'), status:'本轮不调'}), '保护')
  assert.equal(recordStateReason({...row('unknown', '历史'), status:'已核验'}), '历史')
})
