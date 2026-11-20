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

test('price change keeps saved decimals and front-end only adds sign for increases', async () => {
  const { changeLabel, isPositiveChange } = await import('../src/records-model.ts')
  const mk = (change, currency='PHP') => ({ decision: { price_change: change, trace: { station_currency: currency } } })
  assert.equal(changeLabel(mk('3.5')), '+3.5 PHP')
  assert.equal(isPositiveChange(mk('3.5')), false)
  assert.equal(changeLabel(mk('+3.5')), '+3.5 PHP')
  assert.equal(isPositiveChange(mk('+3.5')), true)
  assert.equal(changeLabel(mk('-1.25')), '-1.25 PHP')
  assert.equal(changeLabel(mk('0')), '+0 PHP')
  assert.equal(changeLabel(mk(null)), '未记录')
})

test('dialog log filter keeps full-keyword match across SKU, product and shop, and status filter uses raw status', async () => {
  const { filterRecordItems } = await import('../src/records-model.ts')
  const base = { seller: {id:'a', name:'销售'}, decision: null, execution: null }
  const items = [
    { ...base, id:'1', status:'已核验', shop_name:'TIKTOK-22070PH', key:{ sku:'HHX1983-A1', product_id:'1732512893083550998', item_id:'', shop_id:'1861287085550764034' } },
    { ...base, id:'2', status:'待核验', shop_name:null, key:{ sku:'ABC-9', product_id:'111', item_id:'222', shop_id:'SHOP9' } },
    { ...base, id:'3', status:'待处理', shop_name:'TIKTOK-22070PH', key:{ sku:'ZZZ', product_id:'999', item_id:'', shop_id:'1861287085550764034' } },
  ]
  assert.deepEqual(filterRecordItems(items, '全部', '').map(r=>r.id), ['1','2','3'])
  assert.deepEqual(filterRecordItems(items, '已核验', '').map(r=>r.id), ['1'])
  // 展示别名不参与筛选：待处理状态原值是“待处理”，不能被“活动中”误匹配。
  assert.deepEqual(filterRecordItems(items, '待处理', '').map(r=>r.id), ['3'])
  assert.deepEqual(filterRecordItems(items, '全部', 'hhx1983').map(r=>r.id), ['1'])
  assert.deepEqual(filterRecordItems(items, '全部', 'TIKTOK-22070PH').map(r=>r.id), ['1','3'])
  assert.deepEqual(filterRecordItems(items, '全部', '222').map(r=>r.id), ['2'])
  assert.deepEqual(filterRecordItems(items, '待核验', 'shop9').map(r=>r.id), ['2'])
  assert.deepEqual(filterRecordItems(items, '全部', '没有这个'), [])
})

test('stage table formats saved amounts with sign and unit, never inventing zeros', async () => {
  const { signedDecimal, stagePriceLabel, stageCnyLabel, stageChangeLabel, stagePercentLabel, isIncrease } =
    await import('../src/records-model.ts')
  assert.equal(signedDecimal('1.2331'), '+1.23')
  assert.equal(signedDecimal('-1.23'), '-1.23')
  assert.equal(signedDecimal('0.0000'), '0')
  assert.equal(signedDecimal(null), '—')
  assert.equal(stagePriceLabel({ price: '25.9602' }, 'PHP'), '25.96 PHP')
  assert.equal(stagePriceLabel({ price: null }, 'PHP'), '未记录')
  assert.equal(stageCnyLabel('2.6765'), '2.68 元')
  assert.equal(stageCnyLabel(null), '—')
  assert.equal(stageChangeLabel('1.2331'), '+1.23 元')
  assert.equal(stageChangeLabel(null), '—')
  assert.equal(stagePercentLabel('85.43'), '+85.43%')
  assert.equal(isIncrease('1.2331'), true)
  assert.equal(isIncrease('-0.9184'), false)
  assert.equal(isIncrease('0.0000'), false)
  assert.equal(isIncrease(null), false)
})

test('profit-change column only renders the break-even row and keeps missing evidence empty', async () => {
  const { stageProfitChangeLabel } = await import('../src/records-model.ts')
  // ③ 利润校验行：保存的在线利润校正额折合人民币，正数补 + 号。
  assert.equal(stageProfitChangeLabel({ key: 'break_even', profit_change_cny: '5.7901' }), '+5.79 元')
  // 校验已通过时校正额为 0：显示 0 元，不虚加 + 号。
  assert.equal(stageProfitChangeLabel({ key: 'break_even', profit_change_cny: '0.0000' }), '0 元')
  // 没保存校正额的历史记录显示 —，不推断为 0。
  assert.equal(stageProfitChangeLabel({ key: 'break_even', profit_change_cny: null }), '—')
  // ① ② 行不显示该列内容。
  assert.equal(stageProfitChangeLabel({ key: 'loss', profit_change_cny: '5.7901' }), '—')
  assert.equal(stageProfitChangeLabel({ key: 'original', profit_change_cny: null }), '—')
})

test('records use the requested business labels and four fixed departments', async () => {
  const { recordStatusLabel, recordDepartments } = await import('../src/records-model.ts')
  assert.deepEqual(['已核验','待核验','待处理','本轮不调'].map(recordStatusLabel), ['已提价','待处理','活动中','本轮不调'])
  assert.deepEqual(recordDepartments.map(d => d.name), ['Tiktok一部','Tiktok五部','Tiktok十二部','Tiktok二十二部'])
})


test('all price displays round to at most two places without changing input or long identifiers', async () => {
  const { recordPrice, changeLabel } = await import('../src/records-model.ts')
  const item={decision:{price_change:'3.123456789123456789',trace:{station_currency:'THB'}}}
  assert.equal(recordPrice('23.196',item),'23.2 THB')
  assert.equal(recordPrice('2.584',item),'2.58 THB')
  assert.equal(changeLabel(item),'+3.12 THB')
  assert.equal(item.decision.price_change,'3.123456789123456789')
})
