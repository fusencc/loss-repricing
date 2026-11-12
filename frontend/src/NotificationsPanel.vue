<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { ElPagination } from 'element-plus'
import { api, errorMessage, modeLabel, timeLabel, type Notice, type Item, type ItemStatus } from './api'
import ItemTable from './ItemTable.vue'

type NoticeHeader = Pick<Notice, 'id' | 'batch_id' | 'delivery_state' | 'synthetic' | 'ack_at' | 'created_at' | 'last_error'>
interface NoticeDetail extends NoticeHeader {
  title: string | null
  batch_mode: string | null
  statuses: Partial<Record<ItemStatus, number>>
  total: number
  items: Item[]
}

const props = defineProps<{ admin: boolean; noticeId: string | null }>()
const emit = defineEmits<{ select: [item: Item] }>()
const notices = ref<Notice[]>([])
const detail = ref<NoticeDetail | null>(null)
const busy = ref(false)
const error = ref('')
const status = ref<{ message: string } | null>(null)
const pageSize = 25
const page = ref(1)
let requestVersion = 0

const labels: Record<string, string> = {
  cancelled: '已取消',
  pending: '等待投递',
  sending: '投递处理中',
  accepted: '平台已受理',
  delivered: '平台确认已送达',
  read: '平台确认已读',
  failed: '明确失败',
  unknown: '投递结果未知',
  demo: '演示投递',
}
const itemStatuses: ItemStatus[] = ['待处理', '待核验', '已核验', '本轮不调']

async function load() {
  const version = ++requestVersion
  const noticeId = props.noticeId
  busy.value = true
  error.value = ''
  detail.value = null
  try {
    if (noticeId) {
      const { data } = await api.get<NoticeDetail>('/notifications/' + encodeURIComponent(noticeId), {
        params: { offset: (page.value - 1) * pageSize, limit: pageSize },
      })
      if (version !== requestVersion) return
      const lastPage = Math.max(1, Math.ceil(data.total / pageSize))
      if (page.value > lastPage) {
        page.value = lastPage
        await load()
        return
      }
      detail.value = data
    } else {
      const { data } = await api.get<Notice[]>('/notifications')
      if (version !== requestVersion) return
      notices.value = data
      if (props.admin) {
        const { data } = await api.get<{ message: string }>('/notification-status')
        if (version === requestVersion) status.value = data
      }
    }
  } catch (e) {
    if (version === requestVersion) {
      if (!noticeId) notices.value = []
      error.value = errorMessage(e)
    }
  } finally {
    if (version === requestVersion) busy.value = false
  }
}

async function act(id: Notice['id'], action: 'ack' | 'retry') {
  const version = ++requestVersion
  busy.value = true
  error.value = ''
  try {
    await api.post('/notifications/' + encodeURIComponent(id) + '/' + action)
    if (version === requestVersion) await load()
  } catch (e) {
    if (version === requestVersion) error.value = errorMessage(e)
  } finally {
    if (version === requestVersion) busy.value = false
  }
}

function openNotice(id: Notice['id']) {
  window.location.hash = 'notifications/' + encodeURIComponent(id)
}

watch(() => props.noticeId, () => {
  page.value = 1
  void load()
}, { immediate: true })
onUnmounted(() => { ++requestVersion })
</script>

<template>
  <section v-if="noticeId" class="panel table-panel" v-loading="busy">
    <div class="table-toolbar">
      <div>
        <h2>通知涉及商品</h2>
        <p class="muted">查看本条通知关联商品的处理结果和价格。</p>
      </div>
      <a class="el-button is-text" href="#notifications">返回通知收件箱</a>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <template v-if="detail">
      <div class="table-toolbar">
        <div>
          <strong>{{ detail.title || '亏损提价批次处理结果' }}</strong>
          <p class="muted">{{ modeLabel(detail.batch_mode || '批次来源待核实') }} · {{ timeLabel(detail.created_at) }}</p>
        </div>
        <el-button @click="load">刷新通知详情</el-button>
      </div>
      <p>
        <el-tag :type="detail.synthetic ? 'info' : 'primary'">{{ labels[detail.delivery_state] || detail.delivery_state }}</el-tag>
        <span class="muted"> · 业务确认 {{ detail.ack_at ? timeLabel(detail.ack_at) : '尚未确认' }}</span>
      </p>
      <p v-if="detail.last_error" class="service-error">{{ detail.last_error }}</p>
      <p class="notice-statuses">
        <strong>关联商品 {{ detail.total }}</strong>
        <span v-for="state in itemStatuses" :key="state">{{ state }} {{ detail.statuses[state] ?? 0 }}</span>
      </p>
      <ItemTable :items="detail.items" show-prices empty-text="暂无关联商品" @select="emit('select', $event)" />
      <ElPagination v-if="detail.total > pageSize" v-model:current-page="page"
        small layout="prev, pager, next" :page-size="pageSize" :total="detail.total" @current-change="load" />
      <div class="form-actions">
        <el-button v-if="!detail.ack_at" type="primary" @click="act(detail.id, 'ack')">确认已知悉</el-button>
        <el-button v-if="admin && detail.delivery_state === 'failed'" type="warning" @click="act(detail.id, 'retry')">重试明确失败</el-button>
      </div>
    </template>
  </section>

  <section v-else class="panel table-panel" v-loading="busy">
    <div class="table-toolbar">
      <div>
        <h2>通知收件箱</h2>
        <p class="muted">平台受理、已读、业务确认和价格生效分别记录。</p>
      </div>
      <el-button @click="load">刷新通知</el-button>
    </div>
    <el-alert v-if="status" :title="status.message" type="info" :closable="false" />
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-table :data="notices" empty-text="当前范围暂无通知">
      <el-table-column label="通知" min-width="230">
        <template #default="{ row }">
          <strong>{{ row.summary.title || '亏损提价批次处理结果' }}</strong>
          <p class="muted">商品 {{ row.summary.total ?? row.item_ids.length }} · 异常 {{ row.summary.exceptions ?? '—' }}</p>
          <p v-if="row.last_error" class="service-error">{{ row.last_error }}</p>
        </template>
      </el-table-column>
      <el-table-column label="投递证据" min-width="170">
        <template #default="{ row }">
          <el-tag :type="row.synthetic ? 'info' : 'primary'">{{ labels[row.delivery_state] || row.delivery_state }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="业务确认" min-width="170">
        <template #default="{ row }">{{ row.ack_at ? timeLabel(row.ack_at) : '尚未确认' }}</template>
      </el-table-column>
      <el-table-column label="创建时间" min-width="175">
        <template #default="{ row }">{{ timeLabel(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="155">
        <template #default="{ row }">
          <el-button link type="primary" @click="openNotice(row.id)">查看涉及商品</el-button>
          <el-button v-if="!row.ack_at" link type="primary" @click="act(row.id, 'ack')">确认已知悉</el-button>
          <el-button v-if="admin && row.delivery_state === 'failed'" link type="warning" @click="act(row.id, 'retry')">
            重试明确失败
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </section>
</template>

<style scoped>
.notice-statuses { display: flex; flex-wrap: wrap; gap: 0.4rem 1rem; margin: 1rem 0; }
</style>
