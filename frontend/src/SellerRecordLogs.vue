<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Search } from '@element-plus/icons-vue'
import { formatDecimal } from './decimal-display.ts'
import SkuPriceReferenceView from './SkuPriceReference.vue'
import { recordPrice, recordTone, recordStatusLabel, recordDisplayStates,
  changeLabel, isPositiveChange, isIncrease, filterRecordItems, stagePriceLabel,
  stageChangeLabel, stagePercentLabel, type RecordStatusFilter,
  type SellerGroup } from './records-model'

const props = defineProps<{ seller: SellerGroup }>()

// 筛选只在弹窗内生效：不修改 seller.items 的原始顺序与内容，部门首页统计与下载不受影响。
const statusFilter = ref<RecordStatusFilter>('全部')
const keyword = ref('')
const statuses: RecordStatusFilter[] = ['全部', ...recordDisplayStates]

const visibleItems = computed(() => filterRecordItems(props.seller.items, statusFilter.value, keyword.value))
const emit = defineEmits<{ hitCount: [count: number] }>()
// 弹窗标题播报当前筛选命中数；部门首页统计与下载不受影响。
watch(() => visibleItems.value.length, (count) => emit('hitCount', count), { immediate: true })

// 每次打开人员弹窗重置筛选，避免上一位销售员的条件串到本人日志。
watch(() => props.seller.id, () => {
  statusFilter.value = '全部'
  keyword.value = ''
})

const searchInput = ref<HTMLInputElement | null>(null)
</script>

<template>
  <div class="records-log-scope">
    <div class="records-log-filter" role="group" aria-label="日志筛选">
      <div class="records-log-states">
        <button v-for="state in statuses" :key="state" type="button"
          :aria-pressed="statusFilter === state"
          :class="{ active: statusFilter === state }"
          @click="statusFilter = state">{{ state === '全部' ? '全部' : recordStatusLabel(state) }}</button>
      </div>
      <div class="records-log-search" role="search">
        <input ref="searchInput" v-model="keyword" type="search"
          placeholder="搜索 SKU / 商品 / 店铺" aria-label="按SKU、商品或店铺筛选日志"
          @keydown.esc="keyword = ''" />
        <Search aria-hidden="true" class="records-log-search-icon" />
      </div>
    </div>

    <div class="records-log-list" tabindex="0" aria-label="完整提价日志，可滚动查看">
      <article v-for="item in visibleItems" :key="item.id" class="records-log" :data-record-id="item.id">
        <header class="records-log-heading">
          <div>
            <h3>{{ item.key.sku || 'SKU 未记录' }}</h3>
            <p>{{ item.shop_name || item.key.shop_id }} / {{ item.key.site }} / {{ item.key.platform }}</p>
            <!-- first_margin 只投影唯一匹配的 ERP 订单级「修正一次毛利」原金额及原币；无证据不使用单件亏损或在线利润替代。 -->
            <p>原修正一次毛利：{{ item.first_margin ? `${formatDecimal(item.first_margin.amount)} ${item.first_margin.currency}` : '未记录' }}</p>
          </div>
        </header>
        <div class="records-log-state"><span class="records-status" :class="recordTone(item.status)"><i></i>{{ recordStatusLabel(item.status) }}</span></div>

        <!-- 三环节由后端按保存金额与当日汇率算好：先补亏损，再做利润校验。 -->
        <dl v-if="item.decision?.stages" class="records-log-stages" aria-label="三阶段价格明细">
          <div v-for="stage in item.decision.stages.rows" :key="stage.key" :data-stage="stage.key" class="records-stage-field">
            <dt>{{ stage.label }}</dt>
            <dd>
              <strong>{{ stagePriceLabel(stage, item.decision.stages.currency) }}</strong>
              <small class="records-stage-change">
                <span>比上一环节增加 <span :class="{ 'records-change-up': isIncrease(stage.change_cny) }">{{ stageChangeLabel(stage.change_cny) }}</span></span>
                <span>涨幅 <span :class="{ 'records-change-up': isIncrease(stage.change_percent) }">{{ stagePercentLabel(stage.change_percent) }}</span></span>
              </small>
            </dd>
          </div>
        </dl>
        <!-- 没有三环节证据的历史记录沿用原两价展示，不补造中间环节。 -->
        <dl v-else class="records-log-prices">
          <div><dt>调整前实际价</dt><dd>{{ recordPrice(item.decision?.current_actual, item) }}</dd></div>
          <div class="records-log-target"><dt>价格变动</dt><dd :class="{ 'records-change-up': isPositiveChange(item) }">{{ changeLabel(item) }}</dd></div>
          <div><dt>目标实际价</dt><dd>{{ recordPrice(item.decision?.target_actual, item) }}</dd></div>
        </dl>

        <!-- 执行请求记录的站点币含税上传价，不等同于受理或实际生效；无请求价不以建议价或回读价补齐。 -->
        <dl class="records-log-prices records-log-uploads">
          <div><dt>上传含税价</dt><dd>{{ recordPrice(item.execution?.uploaded_price, item) }}</dd></div>
        </dl>
        <SkuPriceReferenceView :reference="item.decision?.sku_price_reference" :price-limit="item.decision?.price_limit" />
      </article>
      <p v-if="!visibleItems.length" class="records-log-empty" role="status">
        当前筛选没有匹配的日志，可换状态或清空关键词。
      </p>
    </div>
  </div>
</template>
