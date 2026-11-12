<script setup lang="ts">
import type { Item } from './api'
import { statusType, timeLabel } from './api'

defineProps<{ items: Item[]; loading?: boolean; emptyText?: string; showPrices?: boolean }>()
defineEmits<{ select: [item: Item] }>()
function priceLabel(value: unknown, item: Item) {
  if (typeof value !== 'string') return '—'
  const currency = (item.decision?.trace as { station_currency?: string } | undefined)?.station_currency
  return currency ? value + ' ' + currency : value
}
function observedPrice(item: Item) {
  return (item.evidence as { actual_price?: string } | undefined)?.actual_price
}
</script>

<template>
  <el-table v-loading="loading" :data="items" row-key="id" :empty-text="emptyText || '没有符合条件的记录'">
    <el-table-column label="店铺 / 站点" min-width="138">
      <template #default="{ row }">
        <div class="table-primary">{{ row.key.shop_id }}</div>
        <small class="muted">{{ row.key.site }} · {{ row.key.platform }}</small>
      </template>
    </el-table-column>
    <el-table-column label="子 SKU / 仓配" min-width="145">
      <template #default="{ row }">
        <div class="table-primary">{{ row.key.sku }}</div>
        <small class="muted">{{ row.key.warehouse }}</small>
      </template>
    </el-table-column>
    <el-table-column label="状态" width="100">
      <template #default="{ row }"><el-tag :type="statusType(row.status)" effect="light">{{ row.status }}</el-tag></template>
    </el-table-column>
    <el-table-column v-if="showPrices" label="计算目标实际价" min-width="128">
      <template #default="{ row }">{{ priceLabel(row.decision?.target_actual, row) }}</template>
    </el-table-column>
    <el-table-column v-if="showPrices" label="回读实际价" min-width="128">
      <template #default="{ row }">{{ priceLabel(observedPrice(row), row) }}</template>
    </el-table-column>
    <el-table-column prop="reason" label="处理原因" min-width="200" show-overflow-tooltip />
    <el-table-column label="更新时间" min-width="166">
      <template #default="{ row }">{{ timeLabel(row.updated_at) }}</template>
    </el-table-column>
    <el-table-column label="操作" width="85">
      <template #default="{ row }"><el-button type="primary" link @click="$emit('select', row)">查看详情</el-button></template>
    </el-table-column>
  </el-table>
</template>
