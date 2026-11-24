import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
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

test('records page uses live snapshots, guards old departments and releases hidden/unmounted streams', async () => {
  const source = await readFile(new URL('../src/RecordsPage.vue', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /setInterval|60000|api\.get/)
  assert.match(source, /connectRecords<RecordData>\(department.value/)
  assert.match(source, /if \(current !== request\) return/)
  assert.match(source, /data.value = response/)
  assert.match(source, /function onVisibilityChange\(\)[\s\S]*?else stopLive\(\)/)
  assert.match(source, /onUnmounted\(\(\) => \{\s*stopLive\(\)/)
  assert.match(source, /removeEventListener\('visibilitychange', onVisibilityChange\)/)
})
