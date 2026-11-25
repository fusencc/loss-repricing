import test from 'node:test'
import assert from 'node:assert/strict'
import { groupBySeller, effectiveTimeLabel, recordDateLabel } from '../src/records-model.ts'

test('title dates distinguish platform effective time, first verification and record updates in Beijing time', () => {
  const record = (status, execution, updated_at = '2026-11-22T18:00:00Z') => ({ status, execution, updated_at })
  const effective = record('已核验', { effective_time_source: 'effective_at', effective_at: '2026-11-17T18:00:00Z', confirmed_at: '2026-11-20T01:00:00Z' })
  const confirmed = record('已核验', { effective_time_source: 'confirmed_at', effective_at: '2026-11-17T01:00:00Z', confirmed_at: '2026-11-19T01:00:00Z' })
  assert.equal(recordDateLabel([effective]), '提价生效日期：2026/11/18（北京时间）')
  assert.equal(recordDateLabel([confirmed]), '提价核验日期：2026/11/19（北京时间）')
  // 受理、待核验与普通更新时间均不能冒充真实提价；即使存在未终结回执也只展示记录更新日期。
  const pending = record('待核验', { state: 'accepted', effective_at: '2026-11-17T01:00:00Z', confirmed_at: '2026-11-19T01:00:00Z' })
  assert.equal(recordDateLabel([pending]), '记录更新日期：2026/11/23（北京时间）')
  assert.equal(recordDateLabel([record('待处理', null)]), '记录更新日期：2026/11/23（北京时间）')
  const mixed = [pending, confirmed, effective]
  const saved = structuredClone(mixed)
  assert.equal(recordDateLabel(mixed), '提价生效日期：2026/11/18；提价核验日期：2026/11/19；记录更新日期：2026/11/23（北京时间）')
  assert.deepEqual(mixed, saved)
})

test('title dates show a single day or sorted range and explicitly preserve missing or invalid date evidence', () => {
  const record = updated_at => ({ status: '待处理', execution: null, updated_at })
  assert.equal(recordDateLabel([record('2026-11-23T01:00:00Z'), record('2026-11-17T18:00:00Z'), record('2026-11-23T01:00:00Z')]),
    '记录更新日期：2026/11/18 至 2026/11/23（北京时间）')
  for (const value of [undefined, null, '', 'not-a-date', '2026-11-23', '2026-11-23T01:00:00']) {
    assert.equal(recordDateLabel([record(value)]), '提价日期未记录')
  }
  assert.equal(recordDateLabel([]), '提价日期未记录')
  assert.equal(recordDateLabel([record(null), record('2026-11-23T01:00:00+08:00')]), '记录更新日期：2026/11/23；提价日期未记录（北京时间）')
})

const row = (id, owner, status='待核验') => ({
  id, seller: {id:owner,name:'同名销售'}, status,
  execution: status === '已核验' ? { state: 'verified' } : null,
  key: {shop_id:'SHOP'}, decision: {target_actual:'15.123456789123456789'},
})
test('same-name sellers stay separate and all repeated SKU records survive', () => {
  const rows = [row('3','b'),row('2','a'),row('1','b','已核验')]
  const groups = groupBySeller(rows)
  assert.equal(groups.length,2)
  assert.deepEqual(groups[0].items.map(r=>r.id),['3','1'])
  assert.deepEqual(groups[1].items.map(r=>r.id),['2'])
  assert.equal(groups[0].counts['已提价'],1)
  // 待核验并入待处理展示，记录本身与执行证据不改。
  assert.equal(groups[0].counts['待处理'],1)
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

test('dialog log filter keeps full-keyword match across SKU, product and shop, and status filter uses the display category', async () => {
  const { filterRecordItems } = await import('../src/records-model.ts')
  const base = { seller: {id:'a', name:'销售'}, decision: null, execution: null }
  const items = [
    { ...base, id:'1', status:'已核验', execution:{state:'verified'}, shop_name:'TIKTOK-22070PH', key:{ sku:'HHX1983-A1', product_id:'1732512893083550998', item_id:'', shop_id:'1861287085550764034' } },
    { ...base, id:'2', status:'待核验', shop_name:null, key:{ sku:'ABC-9', product_id:'111', item_id:'222', shop_id:'SHOP9' } },
    { ...base, id:'3', status:'待处理', shop_name:'TIKTOK-22070PH', key:{ sku:'ZZZ', product_id:'999', item_id:'', shop_id:'1861287085550764034' } },
  ]
  assert.deepEqual(filterRecordItems(items, '全部', '').map(r=>r.id), ['1','2','3'])
  assert.deepEqual(filterRecordItems(items, '已提价', '').map(r=>r.id), ['1'])
  // 待核验并入待处理筛选，保留原记录及顺序。
  assert.deepEqual(filterRecordItems(items, '待处理', '').map(r=>r.id), ['2','3'])
  assert.deepEqual(filterRecordItems(items, '全部', 'hhx1983').map(r=>r.id), ['1'])
  assert.deepEqual(filterRecordItems(items, '全部', 'TIKTOK-22070PH').map(r=>r.id), ['1','3'])
  assert.deepEqual(filterRecordItems(items, '全部', '222').map(r=>r.id), ['2'])
  assert.deepEqual(filterRecordItems(items, '待处理', 'shop9').map(r=>r.id), ['2'])
  assert.deepEqual(filterRecordItems(items, '全部', '没有这个'), [])
})

test('stage table formats saved amounts with sign and unit, never inventing zeros', async () => {
  const { signedDecimal, stagePriceLabel, stageChangeLabel, stagePercentLabel, isIncrease } =
    await import('../src/records-model.ts')
  assert.equal(signedDecimal('1.2331'), '+1.23')
  assert.equal(signedDecimal('-1.23'), '-1.23')
  assert.equal(signedDecimal('0.0000'), '0')
  assert.equal(signedDecimal(null), '—')
  assert.equal(stagePriceLabel({ price: '25.9602' }, 'PHP'), '25.96 PHP')
  assert.equal(stagePriceLabel({ price: null }, 'PHP'), '未记录')
  assert.equal(stageChangeLabel('1.2331'), '+1.23 元')
  assert.equal(stageChangeLabel(null), '—')
  assert.equal(stagePercentLabel('85.43'), '+85.43%')
  assert.equal(isIncrease('1.2331'), true)
  assert.equal(isIncrease('-0.9184'), false)
  assert.equal(isIncrease('0.0000'), false)
  assert.equal(isIncrease(null), false)
})

test('dialog stages use wrapping horizontal fields with saved price and original margin', async () => {
  const { readFile } = await import('node:fs/promises')
  const source = await readFile(new URL('../src/SellerRecordLogs.vue', import.meta.url), 'utf8')
  const css = await readFile(new URL('../src/records.css', import.meta.url), 'utf8')
  assert.match(source, /<dl class="records-log-stages"/)
  assert.match(source, /<template v-if="item\.decision\?\.stages">/)
  assert.match(source, /<strong[^>]*>\{\{ stagePriceLabel\(stage, item\.decision\.stages\.currency\) \}\}<\/strong>/)
  assert.match(source, /<dt>修正一次毛利<\/dt>/)
  assert.doesNotMatch(source, /records-stage-change|stageChangeLabel|stagePercentLabel/)
  assert.doesNotMatch(source, /<table|在线利润率导致的变动|stageProfitChangeLabel|stage\.profit_change_cny|records-stage-formula|公式：/)
  assert.doesNotMatch(css, /records-stage-table|records-stage-formula/)
  assert.match(css, /\.records-log\{display:grid;grid-template-columns:/)
  assert.match(css, /\.records-log-stages\{display:grid;grid-template-columns:repeat\(6,minmax\(0,1fr\)\)/)
  assert.match(css, /\.records-log-stages\{grid-template-columns:repeat\(2,minmax\(0,1fr\)\)\}/)
  assert.doesNotMatch(css, /records-stage-change/)
})

test('records use the requested business labels and four fixed departments', async () => {
  const { recordStatusLabel, recordDepartments } = await import('../src/records-model.ts')
  assert.deepEqual(['已核验','待核验','待处理','不调价'].map(status => recordStatusLabel({ status, execution: status === '已核验' ? {state:'verified'} : null })), ['已提价','待处理','待处理','不调价'])
  assert.deepEqual(recordDepartments.map(d => d.name), ['Tiktok一部','Tiktok五部','Tiktok十二部','Tiktok二十二部'])
})


test('overview keeps real statuses separate from the unconnected activity placeholder and preserves margin evidence', async () => {
  const { readFile } = await import('node:fs/promises')
  const source = await readFile(new URL('../src/RecordsPage.vue', import.meta.url), 'utf8')
  assert.match(source, /title="算价选中的订单修正一次毛利，按原币展示">修正一次毛利<\/th>/)
  assert.match(source, /class="records-mobile-label">修正一次毛利<\/span>/)
  assert.match(source, /group\.latest\.first_margin\?\.amount/)
  assert.match(source, /group\.latest\.first_margin\.currency/)
  assert.match(source, /countRecordStatuses\(data\.value\?\.items/)
  assert.match(source, /group\.counts\[state\]/)
  assert.match(source, /class="records-activity-placeholder"><i class="neutral"><\/i>活动中<\/span>/)
  assert.doesNotMatch(source, /未接入/)
  assert.doesNotMatch(source, /counts\['活动中'\]|searchExpanded|toggleSearch/)
  assert.match(source, /<input id="records-seller-search" ref="searchInput" v-model="sellerQuery"/)
})

test('pending verification is folded into pending while attention keeps its own count', async () => {
  const { countRecordStatuses, recordDisplayStates, recordTone, filterRecordItems } = await import('../src/records-model.ts')
  assert.deepEqual(recordDisplayStates, ['已提价', '待处理', '不调价'])
  assert.deepEqual(countRecordStatuses([]), { '已提价': 0, '待处理': 0, '不调价': 0 })
  assert.equal(recordTone('待处理'), 'attention')
  const items = [row('1','a','待核验'), row('2','a','待处理'), row('3','a','已核验')]
  const saved = structuredClone(items)
  assert.deepEqual(countRecordStatuses(items), { '已提价': 1, '待处理': 2, '不调价': 0 })
  assert.equal(filterRecordItems(items, '待处理', '').length, 2)
  assert.deepEqual(items, saved)
})

test('all price displays round to at most two places without changing input or long identifiers', async () => {
  const { recordPrice, changeLabel } = await import('../src/records-model.ts')
  const item={decision:{price_change:'3.123456789123456789',trace:{station_currency:'THB'}}}
  assert.equal(recordPrice('23.196',item),'23.2 THB')
  assert.equal(recordPrice('2.584',item),'2.58 THB')
  assert.equal(changeLabel(item),'+3.12 THB')
  assert.equal(item.decision.price_change,'3.123456789123456789')
})


test('only verified execution appears as priced; accepted and unknown fold into pending', async () => {
  const { recordStatusLabel, countRecordStatuses, filterRecordItems } = await import('../src/records-model.ts')
  const accepted = { ...row('1', 'a'), execution: { state: 'accepted' } }
  const verified = { ...row('2', 'a', '已核验'), execution: { state: 'verified' } }
  const unknown = { ...row('3', 'a'), execution: { state: 'unknown' } }
  const mismatch = { ...row('4', 'a'), execution: { state: 'mismatch' } }
  const declined = row('5', 'a', '不调价')
  const items = [accepted, verified, unknown, mismatch, declined]
  assert.equal(recordStatusLabel(accepted), '待处理')
  assert.equal(recordStatusLabel(verified), '已提价')
  assert.equal(recordStatusLabel(unknown), '待处理')
  assert.deepEqual(countRecordStatuses(items), { '已提价': 1, '待处理': 3, '不调价': 1 })
  assert.deepEqual(groupBySeller(items)[0].counts, countRecordStatuses(items))
  assert.deepEqual(filterRecordItems(items, '已提价', '').map(r => r.id), ['2'])
  assert.deepEqual(filterRecordItems(items, '待处理', '').map(r => r.id), ['1', '3', '4'])
  assert.equal(accepted.status, '待核验')
  assert.equal(accepted.execution.state, 'accepted')
})

test('a previous accepted receipt cannot hide a readback mismatch', async () => {
  const { recordStatusLabel, recordStateReason } = await import('../src/records-model.ts')
  const item = { ...row('submitted', 'a'), reason: '回读价格不一致', execution: { state: 'mismatch', submit_status: 'accepted' } }
  assert.equal(recordStatusLabel(item), '待处理')
  assert.equal(recordStateReason(item), '回读核验未通过；回读价格不一致')
  assert.equal(recordStateReason({ ...item, execution: { state: 'accepted' } }), '上传已受理，待回读核验；回读价格不一致')
})
