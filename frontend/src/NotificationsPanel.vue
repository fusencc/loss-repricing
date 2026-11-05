<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { api, errorMessage, timeLabel, type Notice, type Item } from './api'
import ItemTable from './ItemTable.vue'
const emit = defineEmits<{ select: [item: Item] }>()
const linkedItems = ref<Item[]>([])
const selectedNotice = ref<Notice | null>(null)
const itemsBusy = ref(false)
const itemsError = ref('')
let itemRequest = 0
async function showItems(notice: Notice) {
  const request = ++itemRequest
  selectedNotice.value = notice
  linkedItems.value = []
  itemsError.value = ''
  itemsBusy.value = true
  const results = await Promise.allSettled(
    notice.item_ids.map((id) =>
      api.get<Item>(`/items/${encodeURIComponent(id)}`),
    ),
  )
  if (request !== itemRequest) return
  linkedItems.value = results.flatMap((result) =>
    result.status === 'fulfilled' ? [result.value.data] : [],
  )
  if (results.some((result) => result.status === 'rejected'))
    itemsError.value =
      '部分商品已转交或暂时无法读取，请刷新通知确认当前负责范围。'
  itemsBusy.value = false
}
function closeItems() { selectedNotice.value = null; ++itemRequest }
onUnmounted(() => {
  ++itemRequest
})
const props = defineProps<{ admin: boolean }>()
const notices = ref<Notice[]>([]),
  busy = ref(false),
  error = ref('')
const status = ref<{
  message?: string
  mode?: string
  configured?: boolean
} | null>(null)
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
async function load() {
  busy.value = true
  error.value = ''
  try {
    notices.value = (await api.get<Notice[]>('/notifications')).data
    if (props.admin) status.value = (await api.get('/notification-status')).data
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    busy.value = false
  }
}
async function act(n: Notice, action: string) {
  busy.value = true
  error.value = ''
  try {
    await api.post(`/notifications/${encodeURIComponent(n.id)}/${action}`)
    await load()
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    busy.value = false
  }
}
onMounted(load)
</script>
<template>
  <section class="panel table-panel" v-loading="busy">
    <div class="table-toolbar">
      <div>
        <h2>通知收件箱</h2>
        <p class="muted">
          平台受理、已读、业务确认和价格生效分别记录；确认通知不会修改价格。
        </p>
      </div>
      <el-button @click="load">刷新通知</el-button>
    </div>
    <el-alert
      v-if="status"
      :title="
        status.message ||
        (status.mode === 'demo'
          ? '当前为模拟投递，不向真实人员发送。'
          : status.configured
            ? '通知适配器已配置，实际投递状态见各条记录。'
            : '钉钉真实通知未配置。')
      "
      type="info"
      :closable="false"
    />
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-table :data="notices" empty-text="当前范围暂无通知">
      <el-table-column label="通知" min-width="230">
        <template #default="{ row }">
          <strong>
            {{ row.summary.title || `批次 #${row.batch_id ?? '—'} 通知` }}
          </strong>
          <p class="muted">
            商品 {{ row.summary.total ?? row.item_ids.length }} · 异常
            {{ row.summary.exceptions ?? '—' }}
          </p>
          <p v-if="row.last_error" class="service-error">
            {{ row.last_error }}
          </p>
        </template>
      </el-table-column>
      <el-table-column label="投递证据" min-width="170">
        <template #default="{ row }">
          <el-tag :type="row.synthetic ? 'info' : 'primary'">
            {{ labels[row.delivery_state] || row.delivery_state }}
          </el-tag>
          <p class="muted">
            {{
              row.synthetic
                ? '模拟回执，非真实投递'
                : '平台记录；不代表价格生效'
            }}
          </p>
        </template>
      </el-table-column>
      <el-table-column label="业务确认" min-width="180">
        <template #default="{ row }">
          {{ row.ack_at ? timeLabel(row.ack_at) : '尚未确认' }}
        </template>
      </el-table-column>
      <el-table-column label="创建时间" min-width="175">
        <template #default="{ row }">{{ timeLabel(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="155">
        <template #default="{ row }">
          <el-button link type="primary" @click="showItems(row)">
            查看涉及商品
          </el-button>
          <el-button
            v-if="!row.ack_at"
            link
            type="primary"
            @click="act(row, 'ack')"
          >
            确认已知悉
          </el-button>
          <el-button
            v-if="admin && row.delivery_state === 'failed'"
            link
            type="warning"
            @click="act(row, 'retry')"
          >
            重试明确失败
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <section v-if="selectedNotice" class="notice-items">
      <div class="table-toolbar">
        <div>
          <h3>通知涉及商品</h3>
          <p class="muted">仅展示本通知关联且当前账号仍有权限的商品。</p>
        </div>
        <el-button
          text
          @click="closeItems"
        >
          收起商品
        </el-button>
      </div>
      <el-alert
        v-if="itemsError"
        :title="itemsError"
        type="warning"
        :closable="false"
      />
      <ItemTable
        :items="linkedItems"
        :loading="itemsBusy"
        empty-text="暂无可访问的关联商品"
        @select="emit('select', $event)"
      />
    </section>
  </section>
</template>
