import test from 'node:test'
import assert from 'node:assert/strict'
import { groupBySeller, effectiveTimeLabel, initialRecordDepartment, recordDepartments } from '../src/records-model.ts'

test('department opens the explicit link, then the last saved choice, then the first department', () => {
  const [first, fifth, twelfth] = recordDepartments.map(({ id }) => id)
  assert.equal(initialRecordDepartment(null, null), first)
  assert.equal(initialRecordDepartment(null, fifth), fifth)
  assert.equal(initialRecordDepartment(twelfth, fifth), twelfth)
  assert.equal(initialRecordDepartment('unknown', fifth), fifth)
  assert.equal(initialRecordDepartment(null, 'retired'), first)
})

const row = (id, owner, status='待核验') => ({
  id, seller: {id:owner,name:'同名销售'}, status,
  execution: status === '已核验' ? { state: 'verified' } : null,
  key: {shop_id:'SHOP'}, decision: {target_actual:'15.123456789123456789'},
})
test('same-name sellers stay separate and all repeated SKU records survive', () => {
  const rows = [row('3','b'),row('2','a'),row('1','b','已核验')]
  const groups = groupBySeller(rows)
  assert.deepEqual(groups.map(group => group.id), ['seller:a', 'seller:b'])
  const [a, b] = groups
  assert.deepEqual(a.items.map(r=>r.id),['2'])
  assert.deepEqual(b.items.map(r=>r.id),['3','1'])
  assert.equal(b.counts['已提价'],1)
  // 待核验并入待处理展示，记录本身与执行证据不改。
  assert.equal(b.counts['待处理'],1)
  assert.equal(b.latestPriced, rows[2])
  assert.equal(a.latestPriced, null)
  assert.deepEqual(groupBySeller([...rows].reverse()).map(group => group.id), ['seller:a', 'seller:b'])
})

test('seller rows sort by pinyin first character, then full name', () => {
  const people = ['赵安', '张波', '王林', '张安', '陈小', '李雨']
  const rows = people.map((name, i) => ({ ...row(`${i}`, `seller-${i}`), seller: { id: `${i}`, name } }))
  assert.deepEqual(groupBySeller(rows).map(group => group.name), ['陈小', '李雨', '王林', '张安', '张波', '赵安'])
})

test('seller name sorting keeps the first Unicode character whole', () => {
  const collator = new Intl.Collator('zh-Hans-u-co-pinyin')
  const sortedFirsts = ['😀', '🦊'].sort(collator.compare)
  const people = [...sortedFirsts].reverse().map(first => first + '安')
  const rows = people.map((name, i) => ({ ...row(`${i}`, `${i}`), seller: { id: `${i}`, name } }))
  assert.deepEqual(groupBySeller(rows).map(group => group.name), sortedFirsts.map(first => first + '安'))
})

test('seller summary takes only the latest priced record while preserving all records and status counts', () => {
  const rows = [row('5', 'a', '不调价'),
    { ...row('4', 'a'), execution: { state: 'unknown', submit_status: 'accepted' } },
    { ...row('3', 'a'), execution: { state: 'accepted' } }, row('2', 'a', '已核验')]
  const [seller] = groupBySeller(rows)
  assert.equal(seller.latestPriced, rows[2])
  assert.deepEqual(seller.items, rows)
  assert.deepEqual(seller.counts, { '已提价': 2, '待处理': 1, '不调价': 1 })
})
test('unassigned items remain inspectable, and a literal id cannot collide with the unknown group', () => {
  const unknown = { ...row('1', null), seller: { id: null, name: '待分配' } }
  const groups = groupBySeller([unknown, row('2', 'unassigned'), row('3', null)])
  assert.deepEqual(groups.map(group => group.id), ['seller:unassigned', 'unassigned'])
  assert.deepEqual(groups[1].items.map(r => r.id), ['1', '3'])
  assert.deepEqual(groupBySeller([]), [])
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
  assert.match(source, /group\.latestPriced\.first_margin\?\.amount/)
  assert.match(source, /group\.latestPriced\.first_margin\.currency/)
  assert.match(source, /<template v-if="group\.latestPriced">/)
  assert.match(source, /<td v-else class="records-no-priced" colspan="5">暂无已提价记录<\/td>/)
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


test('successful upload is priced while unknown and mismatched results remain pending', async () => {
  const { recordStatusLabel, countRecordStatuses, filterRecordItems } = await import('../src/records-model.ts')
  const accepted = { ...row('1', 'a'), execution: { state: 'accepted' } }
  const verified = { ...row('2', 'a', '已核验'), execution: { state: 'verified' } }
  const unknown = { ...row('3', 'a'), execution: { state: 'unknown' } }
  const mismatch = { ...row('4', 'a'), execution: { state: 'mismatch' } }
  const declined = row('5', 'a', '不调价')
  const items = [accepted, verified, unknown, mismatch, declined]
  assert.equal(recordStatusLabel(accepted), '已提价')
  assert.equal(recordStatusLabel(verified), '已提价')
  assert.equal(recordStatusLabel(unknown), '待处理')
  assert.deepEqual(countRecordStatuses(items), { '已提价': 2, '待处理': 2, '不调价': 1 })
  assert.deepEqual(groupBySeller(items)[0].counts, countRecordStatuses(items))
  assert.deepEqual(filterRecordItems(items, '已提价', '').map(r => r.id), ['1', '2'])
  assert.deepEqual(filterRecordItems(items, '待处理', '').map(r => r.id), ['3', '4'])
  assert.equal(accepted.status, '待核验')
  assert.equal(accepted.execution.state, 'accepted')
})

test('older accepted receipts never mask a later failed, unknown or in-flight execution', async () => {
  const { recordStatusLabel, recordStateReason, countRecordStatuses } = await import('../src/records-model.ts')
  const items = ['failed', 'unknown', 'submitting', 'mismatch'].map((state, i) => ({
    ...row(String(i), 'a'), reason: '具体结果', execution: { state, submit_status: 'accepted' },
  }))
  const saved = structuredClone(items)
  for (const item of items) {
    assert.equal(recordStatusLabel(item), '待处理')
    assert.notEqual(recordStateReason(item), '提价上传完成')
  }
  assert.deepEqual(countRecordStatuses(items), { '已提价': 0, '待处理': 4, '不调价': 0 })
  assert.deepEqual(items, saved)
})

test('a previous accepted receipt cannot hide a readback mismatch', async () => {
  const { recordStatusLabel, recordStateReason } = await import('../src/records-model.ts')
  const item = { ...row('submitted', 'a'), reason: '回读价格不一致', execution: { state: 'mismatch', submit_status: 'accepted' } }
  assert.equal(recordStatusLabel(item), '待处理')
  assert.equal(recordStateReason(item), '上传结果待确认，请勿重复上传')
  assert.equal(recordStateReason({ ...item, execution: { state: 'accepted' } }), '提价上传完成')
})
