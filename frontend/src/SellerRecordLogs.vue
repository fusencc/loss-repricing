<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Search } from '@element-plus/icons-vue'
import { formatDecimal } from './decimal-display.ts'
import SkuPriceReferenceView from './SkuPriceReference.vue'
import RecordIdCopy from './RecordIdCopy.vue'
import { recordPrice, recordTone, recordStatusLabel, recordDisplayStates,
  changeLabel, isPositiveChange, filterRecordItems, stagePriceLabel, recordStageLabel, recordStateReason, recordStatusCount,
  type RecordStatusFilter,
  type SellerGroup } from './records-model'

const props = defineProps<{ seller: SellerGroup }>()

// 筛选只在弹窗内生效：不修改 seller.items 的原始顺序与内容，部门首页统计与下载不受影响。
const statusFilter = ref<RecordStatusFilter>('全部')
const keyword = ref('')
const statuses: RecordStatusFilter[] = ['全部', ...recordDisplayStates]
// 数量固定取该销售员完整记录；状态和关键词只筛选下方内容，不让按钮数字随自身筛选缩减。

const visibleItems = computed(() => filterRecordItems(props.seller.items, statusFilter.value, keyword.value))

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
          @click="statusFilter = state"><i v-if="state !== '全部'" class="records-log-filter-dot" :class="recordTone(state)" aria-hidden="true"></i>{{ state === '全部' ? '全部' : recordStatusLabel(state) }} <span class="records-log-filter-count">{{ state === '全部' ? seller.items.length : recordStatusCount(seller.counts, state) }}</span></button>
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
            <h3><RecordIdCopy v-if="item.key.sku" :value="item.key.sku" label="SKU" /><template v-else>SKU 未记录</template></h3>
            <p>{{ item.shop_name || item.key.shop_id }} / {{ item.key.site }}</p>
            <!-- key.item_id 是规范身份中的具体商品 ID；不拿产品 ID 或主 SKU 补齐，缺失只影响展示。 -->
            <p><RecordIdCopy v-if="item.key.item_id" :value="item.key.item_id" label="商品ID" prefix="商品ID：" :show-icon="false" /><template v-else>商品ID：未记录</template></p>
          </div>
        </header>

        <dl class="records-log-stages" aria-label="毛利与价格明细">
          <!-- first_margin 只投影唯一匹配的 ERP 订单级「修正一次毛利」原金额及原币；无证据不使用单件亏损或在线利润替代。 -->
          <div class="records-stage-field" data-field="first-margin">
            <dt>修正一次毛利</dt>
            <dd><strong>{{ item.first_margin ? `${formatDecimal(item.first_margin.amount)} ${item.first_margin.currency}` : '未记录' }}</strong></dd>
          </div>
          <!-- 三环节由后端按保存金额与当日汇率算好；这里只展示已保存价格，不重算增幅。 -->
          <template v-if="item.decision?.stages">
            <div v-for="stage in item.decision.stages.rows" :key="stage.key" :data-stage="stage.key" class="records-stage-field">
              <dt>{{ recordStageLabel(stage) }}</dt>
              <dd><strong>{{ stagePriceLabel(stage, item.decision.stages.currency) }}</strong></dd>
              <dd v-if="stage.key === 'loss' && stage.trial_profit != null && stage.trial_margin_percent != null">
                <small>按初步补亏后的营销价试算，每件利润 {{ formatDecimal(stage.trial_profit) }} {{ item.decision.stages.currency ?? '币种未记录' }}，利润率 {{ formatDecimal(stage.trial_margin_percent) }}%</small>
              </dd>
            </div>
          </template>
          <!-- 没有三环节证据的历史记录保留原两价和变动，不补造中间环节。 -->
          <template v-else>
            <div class="records-stage-field"><dt>调整前实际价</dt><dd><strong>{{ recordPrice(item.decision?.current_actual, item) }}</strong></dd></div>
            <div class="records-stage-field records-log-target"><dt>价格变动</dt><dd :class="{ 'records-change-up': isPositiveChange(item) }">{{ changeLabel(item) }}</dd></div>
            <div class="records-stage-field"><dt>目标实际价</dt><dd><strong>{{ recordPrice(item.decision?.target_actual, item) }}</strong></dd></div>
          </template>
          <!-- 执行请求记录的站点币含税上传价，与阶段字段同层展示；无请求价不以建议价或回读价补齐。 -->
          <div class="records-stage-field" data-field="uploaded-price">
            <dt>上传含税价</dt>
            <dd><strong>{{ recordPrice(item.execution?.uploaded_price, item) }}</strong></dd>
          </div>
        </dl>

        <!-- 执行状态独立占最右列，不与上传价堆叠；请求金额不等同于受理或实际生效。 -->
        <div class="records-log-state" aria-label="执行状态">
          <span class="records-status" :class="recordTone(item.status)"><i></i>{{ recordStatusLabel(item.status) }}</span>
          <p class="records-log-reason">{{ recordStateReason(item) }}</p>
        </div>
        <SkuPriceReferenceView :reference="item.decision?.sku_price_reference" :price-limit="item.decision?.price_limit" />
      </article>
      <p v-if="!visibleItems.length" class="records-log-empty" role="status">
        当前筛选没有匹配的日志，可换状态或清空关键词。
      </p>
    </div>
  </div>
</template>
