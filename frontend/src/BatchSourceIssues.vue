<script setup lang="ts">
import { ref, shallowRef } from 'vue'
import { ElPagination } from 'element-plus'
import { api, errorMessage, type Identifier, type Item } from './api'

interface SourceIssue {
  source: string
  record_number: number
  key: Pick<Item['key'], 'shop_id' | 'site' | 'sku' | 'product_id' | 'item_id'> & { warehouse: string | null }
  row_id?: string
  main_sku?: string
  related_main_skus?: string[]
}

const props = defineProps<{ batchId: Identifier; total: number }>()
const sourceLabels: Record<string, string> = {
  loss_allocatable: '亏损 · 可配货',
  loss_pending: '亏损 · 待处理',
  blocked_allocatable: '禁止发货 · 可配货',
  blocked_pending: '禁止发货 · 待处理',
  online: '在线商品',
}
const pageSize = 25
const page = ref(1)
const issues = shallowRef<SourceIssue[] | null>(null)
const loading = ref(false)
const error = ref('')

async function load(nextPage: number) {
  if (loading.value) return
  page.value = nextPage
  loading.value = true
  error.value = ''
  issues.value = null
  try {
    const { data } = await api.get<{ items: SourceIssue[] }>(
      '/batches/' + encodeURIComponent(props.batchId) + '/source-issues',
      { params: { offset: (nextPage - 1) * pageSize, limit: pageSize } },
    )
    issues.value = data.items
  } catch (err) {
    error.value = errorMessage(err)
  } finally {
    loading.value = false
  }
}

function onToggle(event: Event) {
  if ((event.currentTarget as HTMLDetailsElement).open && issues.value == null) void load(page.value)
}
</script>

<template>
  <details class="source-issue-panel" @toggle="onToggle">
    <summary>首次取数异常 · {{ total }} 条</summary>
    <p class="muted">按来源和记录序号回查。商品详情保留最近算价时的相关异常。</p>
    <p v-if="loading" role="status">正在读取异常记录…</p>
    <template v-if="error">
      <el-alert :title="error" type="error" :closable="false" />
      <el-button type="primary" link @click="load(page)">重试读取</el-button>
    </template>
    <el-table v-if="issues" :data="issues" empty-text="本页没有异常记录">
      <el-table-column label="来源" min-width="146">
        <template #default="{ row }">{{ sourceLabels[row.source] ?? row.source }}</template>
      </el-table-column>
      <el-table-column label="记录序号" prop="record_number" width="96" />
      <el-table-column label="店铺 / 站点" min-width="128" show-overflow-tooltip>
        <template #default="{ row }">{{ row.key.shop_id }} / {{ row.key.site }}</template>
      </el-table-column>
      <el-table-column label="产品 ID / 商品 ID" min-width="170" show-overflow-tooltip>
        <template #default="{ row }">{{ row.key.product_id }} / {{ row.key.item_id }}</template>
      </el-table-column>
      <el-table-column label="子 SKU" prop="key.sku" min-width="140" show-overflow-tooltip />
      <el-table-column label="仓配" min-width="100" show-overflow-tooltip>
        <template #default="{ row }">{{ row.key.warehouse ?? '缺失' }}</template>
      </el-table-column>
      <el-table-column label="主 SKU" min-width="140" show-overflow-tooltip>
        <template #default="{ row }">{{ [row.main_sku, ...(row.related_main_skus ?? [])].filter(Boolean).join(' / ') }}</template>
      </el-table-column>
      <el-table-column label="订单明细" prop="row_id" min-width="165" show-overflow-tooltip />
    </el-table>
    <ElPagination v-if="total > pageSize" :total="total" :page-size="pageSize"
      :current-page="page" :disabled="loading" layout="prev, pager, next" small @current-change="load" />
  </details>
</template>

<style scoped>
.source-issue-panel { margin: 0 19px 16px; min-width: 0; overflow: hidden; }
.source-issue-panel summary { cursor: pointer; font-weight: 650; }
.source-issue-panel p { margin: 8px 0; }
.source-issue-panel :deep(.el-pagination) { margin-top: 10px; }
</style>
