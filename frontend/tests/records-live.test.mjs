import test from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'
import { createRenderer, ssrContextKey } from 'vue'
import { recordDepartments } from '../src/records-model.ts'
import { connectRecords } from '../src/records-live.ts'

test('live records receive persisted snapshots, report interruption and close the connection', () => {
  const original = globalThis.EventSource
  let source
  globalThis.EventSource = class {
    constructor(url) { this.url = url; this.closed = false; source = this }
    close() { this.closed = true }
  }
  try {
    const snapshots = []
    let errors = 0
    const close = connectRecords('department:Tiktok一部', data => snapshots.push(data), () => errors++)
    const url = new URL(source.url, 'http://localhost')
    assert.equal(url.pathname, '/api/records/stream')
    assert.equal(url.searchParams.get('department'), 'department:Tiktok一部')
    source.onmessage({ data: JSON.stringify({ items: [{ status: '待核验' }] }) })
    source.onmessage({ data: JSON.stringify({ items: [{ status: '已核验' }] }) })
    assert.deepEqual(snapshots.map(data => data.items[0].status), ['待核验', '已核验'])
    source.onerror()
    assert.equal(errors, 1)
    assert.equal(source.closed, false, 'native EventSource remains available for automatic reconnect')
    source.onmessage({ data: 'invalid json' })
    assert.equal(errors, 2)
    assert.equal(snapshots.length, 2)
    close()
    assert.equal(source.closed, true)
  } finally {
    globalThis.EventSource = original
  }
})

test('records page keeps only visited snapshots and isolates late messages across switches and visibility changes', async t => {
  const server = await createServer({ server: { middlewareMode: true, hmr: false, ws: false }, appType: 'custom' })
  t.after(() => server.close())
  const { default: component } = await server.ssrLoadModule('/src/RecordsPage.vue')
  const document = Object.assign(new EventTarget(), { visibilityState: 'visible' })
  const preferences = new Map()
  const sources = []
  const originals = ['document', 'window', 'EventSource'].map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)])
  t.after(() => {
    for (const [name, descriptor] of originals) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor)
      else delete globalThis[name]
    }
  })
  Object.assign(globalThis, {
    document,
    window: { location: { search: '' }, localStorage: { getItem: key => preferences.get(key), setItem: (key, val) => preferences.set(key, val) } },
    EventSource: class {
      constructor(url) {
        this.department = new URL(url, 'http://localhost').searchParams.get('department')
        this.closed = false
        sources.push(this)
      }
      close() { this.closed = true }
      emit(data) { this.onmessage({ data: JSON.stringify(data) }) }
    },
  })
  // Mount the actual page setup/lifecycle; its template is covered separately from these stream events.
  const renderer = createRenderer({ createComment: () => ({}), insert() {}, remove() {} })
  const app = renderer.createApp({ ...component, render: () => null }, { mode: 'demo' }).provide(ssrContextKey, {})
  const page = app.mount({}).$.setupState
  t.after(() => { if (app._instance) app.unmount() })
  const [first, fifth, twelfth] = recordDepartments.map(({ id }) => id)
  const item = name => ({ id: name, seller: { id: name, name }, status: '不调价' })
  const snapshot = (department, items = []) => ({ department, items, total: items.length })
  const firstSaved = snapshot(first, [item('甲')])

  assert.deepEqual(sources.map(source => source.department), [first], 'do not prefetch other departments')
  assert.equal(page.loading, true)
  assert.equal(page.refreshing, false)
  sources[0].emit(firstSaved)
  page.sellerQuery = '甲'
  page.selectedSeller = 'seller:甲'
  page.dialogOpen = true

  page.changeDepartment(fifth)
  assert.equal(sources[0].closed, true)
  assert.equal(page.data, null)
  assert.equal(page.loading, true)
  assert.equal(page.dialogOpen, false)
  assert.equal(page.selectedSeller, null)
  assert.equal(page.sellerQuery, '')
  sources[0].emit(snapshot(first, [item('迟到')]))
  sources[1].emit(firstSaved) // A current stream cannot write another department's frame.
  assert.equal(page.department, fifth)
  assert.equal(page.data, null)
  sources[1].onerror()
  assert.equal(page.loading, false)
  assert.match(page.error, /正在重新连接/)
  assert.equal(sources[1].closed, false, 'leave automatic reconnect to EventSource')
  const empty = snapshot(fifth)
  sources[1].emit(empty)

  page.changeDepartment(first)
  assert.deepEqual(page.data, firstSaved)
  assert.equal(page.loading, false)
  assert.equal(page.refreshing, true)
  sources[0].emit(snapshot(first, [item('同部门旧连接')]))
  sources[1].onerror()
  assert.equal(page.error, '')
  assert.deepEqual(page.data, firstSaved)
  const latest = snapshot(first, [item('甲'), item('乙')])
  sources[2].emit(latest)
  assert.equal(page.refreshing, false)
  sources[2].onerror()
  assert.match(page.error, /正在重新连接/)
  assert.deepEqual(page.data, latest)

  document.visibilityState = 'hidden'
  document.dispatchEvent(new Event('visibilitychange'))
  assert.equal(sources[2].closed, true)
  page.changeDepartment(fifth)
  assert.deepEqual(page.data, empty, 'an empty successful snapshot is still reusable')
  assert.equal(page.loading, false)
  assert.equal(page.refreshing, false)
  assert.equal(page.error, '')
  assert.equal(sources.length, 3, 'no new connection while hidden')
  document.visibilityState = 'visible'
  document.dispatchEvent(new Event('visibilitychange'))
  assert.equal(page.refreshing, true)
  assert.equal(sources[3].department, fifth)
  sources[2].emit(snapshot(first, [item('隐藏后迟到')]))
  sources[3].emit(snapshot(fifth, [item('丙')]))
  assert.equal(page.refreshing, false)
  assert.equal(page.data.items[0].id, '丙')
  assert.deepEqual([...preferences.values()], [fifth], 'only the department preference is stored')

  page.changeDepartment(first)
  assert.deepEqual(page.data, latest, 'store only the most recent success for each department')
  page.changeDepartment(twelfth)
  assert.equal(page.data, null)
  assert.equal(page.loading, true)
  assert.deepEqual(sources.map(source => source.department), [first, fifth, first, fifth, first, twelfth])
  assert.ok(sources.slice(0, -1).every(source => source.closed))
  app.unmount()
  assert.equal(sources.at(-1).closed, true)
  sources.at(-1).emit(snapshot(twelfth))
  document.dispatchEvent(new Event('visibilitychange'))
  assert.equal(page.data, null)
  assert.equal(sources.length, 6)
})
