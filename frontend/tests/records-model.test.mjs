import test from 'node:test'
import assert from 'node:assert/strict'
import { groupBySeller, effectiveTimeLabel } from '../src/records-model.ts'

const row = (id, owner, status='待核验') => ({
  id, seller: {id:owner,name:'同名销售'}, status,
  key: {shop_id:'SHOP'}, decision: {target_actual:'15.123456789123456789'},
})
test('same-name sellers stay separate and all repeated SKU records survive', () => {
  const rows = [row('3','b'),row('2','a'),row('1','b','已核验')]
  const groups = groupBySeller(rows)
  assert.equal(groups.length,2)
  assert.deepEqual(groups[0].items.map(r=>r.id),['3','1'])
  assert.deepEqual(groups[1].items.map(r=>r.id),['2'])
  assert.equal(groups[0].counts['已核验'],1)
  assert.equal(groups[0].counts['待核验'],1)
  assert.equal(groups[0].latest.decision.target_actual,'15.123456789123456789')
})
test('unassigned items remain inspectable, and a literal id cannot collide with the unknown group', () => {
  const groups=groupBySeller([row('1',null),row('2','unassigned'),row('3',null)])
  assert.equal(groups.length,2)
  assert.notEqual(groups[0].id,groups[1].id)
  assert.deepEqual(groups[0].items.map(r=>r.id),['1','3'])
  assert.deepEqual(groupBySeller([]),[])
})
test('confirmed time used for protection is never presented as exact price effective time', () => {
  assert.equal(effectiveTimeLabel('confirmed_at'),'保护起算时间（按首次核验）')
  assert.equal(effectiveTimeLabel('effective_at'),'来源记录的生效时间')
  assert.equal(effectiveTimeLabel(null),'保护起算时间（来源未记录）')
})

test('records use the requested business labels and four fixed departments', async () => {
  const { recordStatusLabel, recordDepartments } = await import('../src/records-model.ts')
  assert.deepEqual(['已核验','待核验','待处理','本轮不调'].map(recordStatusLabel), ['已提价','待处理','活动中','本轮不调'])
  assert.deepEqual(recordDepartments.map(d => d.name), ['Tiktok一部','Tiktok五部','Tiktok十二部','Tiktok二十二部'])
})
