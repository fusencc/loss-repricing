import test from 'node:test'
import assert from 'node:assert/strict'
import { recordStateReason, recordStatusLabel } from '../src/records-model.ts'

test('in-flight and uncertain uploads never instruct the seller to reupload', () => {
  for (const state of ['unknown', 'submitting', 'mismatch']) {
    const item = { status: '待核验', execution: { state, submit_status: 'unknown' } }
    assert.equal(recordStatusLabel(item), '待处理')
    assert.equal(recordStateReason(item), '改价结果待确认，请到店铺后台查看，勿重复改价')
  }
  assert.equal(recordStateReason({status: '待核验', phase: 'blocked'}), '改价结果待确认，请到店铺后台查看，勿重复改价')
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
    assert.equal(recordStateReason(item), execution.row_status === 'rejected' || execution.submit_status === 'rejected'
      ? '改价未通过，请核对商品和要改的售价' : '改价未提交成功，请把商品 ID 发给技术')
  }
  assert.equal(recordStateReason({status: '不调价', reason: '近72小时内有调价记录'}), '近72小时内有调价记录')
  assert.equal(recordStateReason({status: '不调价', reason: ''}), '原因未记录')
})

test('accepted department readback mismatch stays priced', () => {
  const item = {status: '待核验', execution: {state: 'mismatch', submit_status: 'accepted'}}
  assert.equal(recordStatusLabel(item), '已提价')
  assert.equal(recordStateReason(item), '提价上传完成')
})


test('pending records use fixed seller instructions for all observed causes', () => {
  for (const [reason, expected] of [
    ['站点价格档位导致实际价无效或超出误差容限', '系统算出的售价不符合站点规则'],
    ['提交前商品、仓配或现价与算价依据不一致（排查编号 synthetic）', '改价前商品信息对不上仓'],
    ['候选明细未匹配到当前在线子 SKU', '系统没找到对应的 SKU'],
    ['平台、店铺、产品、商品、子 SKU 或仓配映射缺失', '系统没找到对应的 SKU'],
    ['本站点与仓配组合缺少已验证的单品上传和回读证据', '这个站点和发货仓暂不能自动改价'],
    ['提交前刷新后候选已消失', '复查时没找到待改价商品'],
    ['提交前无法确认单品当前价格或操作日志（排查编号 synthetic）', '售价或改价记录未确认'],
  ]) {
    const item = { status: '待处理', execution: null, reason }
    assert.equal(recordStateReason(item), expected)
    assert.ok([...expected].length <= 32)
    assert.equal(item.reason, reason)
  }
})

test('unrecognized, long and blank reasons all use the same fixed instruction', () => {
  for (const reason of ['缺少当天汇率', '后台日志'.repeat(30) + '\n第二行', '', undefined]) {
    assert.equal(recordStateReason({status: '待处理', reason}), '暂未查明原因，请把商品 ID 发给技术')
  }
})

test('an explicit rejection takes precedence over old preparation or readback details', () => {
  const item = {status: '待核验', reason: '候选明细未匹配到当前在线子 SKU',
    execution: {state: 'mismatch', row_status: 'rejected'}}
  assert.equal(recordStateReason(item), '改价未通过，请核对商品和要改的售价')
})


test('Vietnam history displays the saved rounding gap and the old limit', () => {
  const reason = '站点价格档位导致实际价无效或超出误差容限'
  const item = {status: '待处理', reason, decision: {vietnam_rounding_gap: {amount: '26.1534', limit: '2'}}}
  const text = recordStateReason(item)
  assert.equal(text, '取整后与目标价约差 26.15 越南盾（旧上限 2 盾）')
  assert.ok([...text].length <= 32)
  item.decision.vietnam_rounding_gap.amount = '2.0001'
  assert.equal(recordStateReason(item), '取整后价差超过 2 越南盾（旧规则）')
})
