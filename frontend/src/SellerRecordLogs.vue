<script setup lang="ts">
import { recordPrice, recordTone, recordStatusLabel, type SellerGroup } from './records-model'
defineProps<{ seller: SellerGroup }>()
</script>

<template>
  <div class="records-log-list" tabindex="0" aria-label="完整提价日志，可滚动查看">
    <article v-for="item in seller.items" :key="item.id" class="records-log" :data-record-id="item.id">
      <header class="records-log-heading"><div><h3>{{ item.key.sku || 'SKU 未记录' }}</h3><p>{{ item.shop_name || item.key.shop_id }} / {{ item.key.site }} / {{ item.key.platform }}</p></div><div class="records-log-state"><span class="records-status" :class="recordTone(item.status)"><i></i>{{ recordStatusLabel(item.status) }}</span></div></header>
      <dl class="records-log-prices">
        <div><dt>调整前实际价</dt><dd>{{ recordPrice(item.decision?.current_actual, item) }}</dd></div>
        <div><dt>一次毛利</dt><dd>{{ item.first_margin ? `${item.first_margin.amount} ${item.first_margin.currency}` : '未记录' }}</dd></div>
        <div class="records-log-target"><dt>目标实际价</dt><dd>{{ recordPrice(item.decision?.target_actual, item) }}</dd></div>
        <div><dt>建议上传价</dt><dd>{{ recordPrice(item.decision?.upload_price, item) }}</dd></div>
        <div><dt>提交记录价</dt><dd>{{ recordPrice(item.execution?.uploaded_price, item) }}</dd></div>
        <div><dt>回读实际价</dt><dd>{{ recordPrice(item.execution?.actual_price, item) }}</dd></div>
      </dl>
    </article>
  </div>
</template>
