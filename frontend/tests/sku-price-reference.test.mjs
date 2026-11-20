import test, { after, before } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'

let server, Component
before(async () => {
  server = await createServer({ server: { middlewareMode: true }, appType: 'custom' })
  Component = (await server.ssrLoadModule('/src/SkuPriceReference.vue')).default
})
after(async () => { await server?.close() })

test('missing optional reference has no section or pending message', async () => {
  const html = await renderToString(createSSRApp(Component, { reference: null }))
  assert.doesNotMatch(html, /参考价|待处理|不足/)
})

test('saved amount and three source rows render without recalculating prices', async () => {
  const reference = { marketing_price: '42.91333333333333333333333333333', currency: 'PHP',
    samples: ['A', 'B', 'C'].map(shop_name => ({ shop_name, product_id: 'P' + shop_name,
      item_id: 'I' + shop_name, sales_7d: 3, tax_inclusive_price: '100',
      discount_percent: '50', marketing_price: '50' })) }
  const html = await renderToString(createSSRApp(Component, { reference }))
  assert.match(html, /42\.91 PHP/)
  assert.match(html, /同 SKU Top3 营销参考价/)
  for (const shop of ['A', 'B', 'C']) assert.match(html, new RegExp('>' + shop + '<'))
  // 样本手算均值为50；展示仍必须用后端保存的42.9133，前端不能重算。
  assert.doesNotMatch(html, /价格上限|待处理/)
})

test('active price cap shows the saved reduction and is not described as advisory only',async()=>{
  const reference={marketing_price:'10.5',currency:'MYR',samples:[]}
  const priceLimit={upper_actual:'10.5',applied:true,reduction_amount:'0.7'}
  const html=await renderToString(createSSRApp(Component,{reference,priceLimit}))
  assert.match(html,/目标营销价上限/)
  assert.match(html,/0\.7 MYR/)
  assert.match(html,/参考价优先/)
  assert.doesNotMatch(html,/本轮不调|利润要求/)
  assert.doesNotMatch(html,/仅供参考/)
})

test('reference priority is displayed when no finite one-percent floor exists',async()=>{
  const reference={marketing_price:'9',currency:'MYR',samples:[]}
  const priceLimit={upper_actual:'9',applied:true,reduction_amount:null}
  const html=await renderToString(createSSRApp(Component,{reference,priceLimit}))
  assert.match(html,/参考价优先/)
  assert.doesNotMatch(html,/本轮不调|null|利润要求/)
})
