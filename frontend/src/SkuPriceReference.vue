<script setup lang="ts">
import type { SkuPriceReference, PriceLimit } from './api'
import { formatDecimal } from './decimal-display.ts'

defineProps<{ reference: SkuPriceReference | null | undefined; priceLimit?: PriceLimit | null }>()
</script>

<template>
  <section v-if="reference" class="sku-price-reference">
    <p><strong>同 SKU Top3 营销参考价</strong>：{{ formatDecimal(reference.marketing_price) }} {{ reference.currency }}</p>
    <p class="sku-reference-note">近 7 天销量 Top3 的营销价均值<span v-if="priceLimit">，作为目标营销价上限</span>。</p>
    <p v-if="priceLimit" class="sku-reference-note">参考价优先；利润不足 1% 或需要降价时，仍按参考上限调整。</p>
    <p v-if="priceLimit?.applied && priceLimit.reduction_amount" class="sku-reference-note">
      已按上限压减 {{ formatDecimal(priceLimit.reduction_amount) }} {{ reference.currency }}。
    </p>
    <details>
      <summary>查看三条参考商品</summary>
      <div class="sku-reference-table">
        <table>
          <thead><tr><th>店铺</th><th>7 天销量</th><th>税后价</th><th>折扣减免</th><th>营销价</th></tr></thead>
          <tbody>
            <tr v-for="sample in reference.samples" :key="`${sample.shop_name}/${sample.product_id}/${sample.item_id}`">
              <td>{{ sample.shop_name }}</td><td>{{ sample.sales_7d }}</td>
              <td>{{ formatDecimal(sample.tax_inclusive_price) }}</td>
              <td>{{ formatDecimal(sample.discount_percent) }}%</td>
              <td>{{ formatDecimal(sample.marketing_price) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="sku-reference-note">每条营销价＝税后价 ×（100－折扣）÷100。</p>
    </details>
  </section>
</template>

<style scoped>
.sku-price-reference { margin: 16px 0; font-size: 14px; line-height: 1.6; }
.sku-price-reference p { margin: 6px 0; }
.sku-reference-note { opacity: .75; font-size: 12px; }
summary { cursor: pointer; }
.sku-reference-table { max-width: 100%; overflow-x: auto; }
table { width: 100%; margin-top: 8px; border-collapse: collapse; font-size: 12px; }
th, td { padding: 6px 8px; text-align: left; white-space: nowrap; border-bottom: 1px solid var(--line, #cbd5e1); }
</style>
