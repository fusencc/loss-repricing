<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import {
  api,
  errorMessage,
  timeLabel,
  display,
  type Item,
  type Correction,
} from './api'
const props = defineProps<{ item: Item; actorId: string; actorRole: string }>()
const emit = defineEmits<{ refresh: [] }>()
const corrections = ref<Correction[]>([]),
  reason = ref(''),
  desired = ref(''),
  busy = ref(false),
  error = ref('')
let pollTimer: ReturnType<typeof setTimeout> | undefined
let pollsRemaining = 30
let requestVersion = 0
let disposed = false
function stopPolling() {
  clearTimeout(pollTimer)
  pollTimer = undefined
}
async function load(resetBudget = false) {
  stopPolling()
  if (resetBudget) pollsRemaining = 30
  const request = ++requestVersion
  const itemId = props.item.id
  try {
    const { data } = await api.get<Correction[]>(`/items/${itemId}/corrections`)
    if (disposed || request !== requestVersion || itemId !== props.item.id)
      return
    corrections.value = data
    error.value = ''
    if (
      data.some((c) => ['queued', 'executing'].includes(c.state)) &&
      pollsRemaining > 0
    ) {
      --pollsRemaining
      pollTimer = setTimeout(() => {
        void load()
      }, 2000)
    }
  } catch (e) {
    if (!disposed && request === requestVersion) error.value = errorMessage(e)
  }
}
watch(
  () => props.item.id,
  () => {
    reason.value = ''
    desired.value = ''
    corrections.value = []
    error.value = ''
    void load(true)
  },
  { immediate: true },
)
watch(
  () => props.item.control?.version,
  () => {
    void load(true)
  },
)
onUnmounted(() => {
  disposed = true
  ++requestVersion
  stopPolling()
})
async function control(action: string) {
  await perform(() =>
    api.post(`/items/${props.item.id}/control`, {
      action,
      reason: reason.value,
      version: props.item.control?.version ?? 0,
    }),
  )
}
async function perform(fn: () => Promise<unknown>) {
  busy.value = true
  error.value = ''
  try {
    await fn()
    reason.value = ''
    desired.value = ''
    emit('refresh')
    await load(true)
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    busy.value = false
  }
}
function propose() {
  void perform(() =>
    api.post(`/items/${props.item.id}/corrections`, {
      desired_actual: desired.value,
      reason: reason.value,
      version: props.item.control?.version ?? 0,
    }),
  )
}
function approve(c: Correction) {
  void perform(() =>
    api.post(`/corrections/${c.id}/approve`, {
      version: props.item.control?.version ?? 0,
    }),
  )
}
const labels: Record<string, string> = {
  proposed: '待批准',
  queued: '已排队',
  executing: '执行中',
  uploaded: '上传完成',
  verified: '已核验',
  failed: '失败',
  stale: '已失效',
}
</script>
<template>
  <section class="drawer-section" v-loading="busy">
    <h3>负责与接管</h3>
    <dl class="detail-grid">
      <div>
        <dt>负责人 / 主管</dt>
        <dd>
          {{
            item.ownership?.owner_name ||
            (item.ownership?.owner_id === 'admin'
              ? '管理员'
              : item.ownership?.owner_id) ||
            '待分配'
          }}
          /
          {{
            item.ownership?.supervisor_name ||
            item.ownership?.supervisor_id ||
            '—'
          }}
        </dd>
      </div>
      <div>
        <dt>管理状态 · 版本 {{ item.control?.version ?? 0 }}</dt>
        <dd>
          {{
            { auto: '自动托管', manual: '人工接管', review: '待协调' }[
              item.control?.mode || 'auto'
            ]
          }}
        </dd>
      </div>
    </dl>
    <p class="muted">
      人工接管或暂停仅阻止尚未发送的自动意图；在途及结果未知请求继续核验。交回自动后重新读取基线，仍保留执行锁。
    </p>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-input
      v-model="reason"
      type="textarea"
      placeholder="填写接管、暂停、交回或修正原因（必填）"
      maxlength="500"
    />
    <div class="spaced">
      <el-button
        v-if="item.capabilities?.takeover"
        :disabled="!reason.trim()"
        @click="control('takeover')"
      >
        人工接管
      </el-button>
      <el-button
        v-if="item.capabilities?.pause"
        :disabled="!reason.trim()"
        @click="control('pause')"
      >
        暂停并协调
      </el-button>
      <el-button
        v-if="item.capabilities?.handback"
        :disabled="!reason.trim()"
        @click="control('handback')"
      >
        明确交回自动
      </el-button>
    </div>
    <div v-if="item.capabilities?.propose_correction" class="spaced">
      <h3>提交人工修正</h3>
      <p class="muted">
        填写目标实际售价（站点币）。保存即人工接管，批准仅排队；后台按最新价格、折扣、仓配与费用重新检查。
      </p>
      <el-input
        v-model="desired"
        placeholder="目标实际售价，例如 12.50"
        inputmode="decimal"
      />
      <el-button
        class="spaced"
        type="primary"
        :disabled="!reason.trim() || !/^\d+(\.\d+)?$/.test(desired)"
        @click="propose"
      >
        保存修正方案
      </el-button>
    </div>
    <div class="spaced">
      <h3>修正记录</h3>
      <el-button text type="primary" @click="load(true)">
        刷新修正状态
      </el-button>
      <p class="muted">
        已保存的价格最多显示两位小数，审批仍按提交时的原值执行。排队或执行中的方案会短时自动刷新；可随时手动刷新，保留尚未提交的输入。
      </p>
    </div>
    <el-table :data="corrections" empty-text="暂无修正方案">
      <el-table-column label="目标实际价">
        <template #default="{ row }">{{ display(row.desired_actual) }}</template>
      </el-table-column>
      <el-table-column label="状态">
        <template #default="{ row }">
          {{ labels[row.state] || row.state }}
        </template>
      </el-table-column>
      <el-table-column prop="reason" label="原因" show-overflow-tooltip />
      <el-table-column label="操作">
        <template #default="{ row }">
          <el-button
            v-if="
              item.capabilities?.approve_correction &&
              row.state === 'proposed' &&
              (actorRole !== 'sales' || row.requested_by === actorId)
            "
            link
            type="primary"
            @click="approve(row)"
          >
            批准并排队
          </el-button>
        </template>
      </el-table-column>
      <el-table-column type="expand">
        <template #default="{ row }">
          <p>{{ timeLabel(row.created_at) }} · {{ row.requested_by }}</p>
          <pre>{{ display(row.result) }}</pre>
        </template>
      </el-table-column>
    </el-table>
    <p class="muted">
      检测到外部价格、折扣或仓配变化时，原方案会暂停；核对后请重新取数提交方案。
    </p>
  </section>
</template>
