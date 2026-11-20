<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Search } from '@element-plus/icons-vue'
import { formatDecimal } from './decimal-display.ts'
import { recordPrice, recordTone, recordStatusLabel,
  changeLabel, isPositiveChange, isIncrease, filterRecordItems, stagePriceLabel, stageCnyLabel,
  stageChangeLabel, stagePercentLabel, stageProfitChangeLabel, type RecordStatusFilter,
  type SellerGroup } from './records-model'

const props = defineProps<{ seller: SellerGroup }>()

// 筛选只在弹窗内生效：不修改 seller.items 的原始顺序与内容，部门首页统计与下载不受影响。
const statusFilter = ref<RecordStatusFilter>('全部')
const keyword = ref('')
const statuses: RecordStatusFilter[] = ['全部', '已核验', '待核验', '待处理', '本轮不调']

const visibleItems = computed(() => filterRecordItems(props.seller.items, statusFilter.value, keyword.value))
// 计数始终描述当前筛选条件命中的记录数，与部门页的四态汇总分开。
const hitCount = computed(() => visibleItems.value.length)

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
    <p class="records-log-count" role="status">
      共 {{ seller.items.length }} 条记录<span v-if="hitCount !== seller.items.length">，当前筛选命中 {{ hitCount }} 条</span>
    </p>

    <div class="records-log-list" tabindex="0" aria-label="完整提价日志，可滚动查看">
      <article v-for="item in visibleItems" :key="item.id" class="records-log" :data-record-id="item.id">
        <header class="records-log-heading"><div><h3>{{ item.key.sku || 'SKU 未记录' }}</h3><p>{{ item.shop_name || item.key.shop_id }} / {{ item.key.site }} / {{ item.key.platform }}</p></div><div class="records-log-state"><span class="records-status" :class="recordTone(item.status)"><i></i>{{ recordStatusLabel(item.status) }}</span></div></header>

        <!-- 三环节由后端按保存金额与当日汇率算好：先补亏损，再做利润校验；
             「在线利润率导致的变动」取保存的在线利润校正额，只在 ③ 行有值。 -->
        <table v-if="item.decision?.stages" class="records-stage-table">
          <caption class="records-sr">调价环节拆分：原实际营销价、补亏损、利润校验</caption>
          <thead><tr>
            <th scope="col">环节</th>
            <th scope="col">{{ item.decision.stages.currency || '站点币' }}</th>
            <th scope="col">折合人民币</th>
            <th scope="col">比上一环节增加</th>
            <th scope="col">比上一环节涨幅</th>
            <th scope="col">在线利润率导致的变动</th>
          </tr></thead>
          <tbody>
            <tr v-for="stage in item.decision.stages.rows" :key="stage.key" :data-stage="stage.key">
              <th scope="row">{{ stage.label }}</th>
              <td>{{ stagePriceLabel(stage, item.decision.stages.currency) }}</td>
              <td>{{ stageCnyLabel(stage.cny) }}</td>
              <td :class="{ 'records-change-up': isIncrease(stage.change_cny) }">{{ stageChangeLabel(stage.change_cny) }}</td>
              <td :class="{ 'records-change-up': isIncrease(stage.change_percent) }">{{ stagePercentLabel(stage.change_percent) }}</td>
              <td :class="{ 'records-change-up': isIncrease(stage.profit_change_cny) }">{{ stageProfitChangeLabel(stage) }}</td>
            </tr>
          </tbody>
        </table>
        <!-- 没有三环节证据的历史记录沿用原两价展示，不补造中间环节。 -->
        <dl v-else class="records-log-prices">
          <div><dt>调整前实际价</dt><dd>{{ recordPrice(item.decision?.current_actual, item) }}</dd></div>
          <div class="records-log-target"><dt>价格变动</dt><dd :class="{ 'records-change-up': isPositiveChange(item) }">{{ changeLabel(item) }}</dd></div>
          <div><dt>目标实际价</dt><dd>{{ recordPrice(item.decision?.target_actual, item) }}</dd></div>
        </dl>

        <!-- 三环节公式简述：金额仍由后端保存值展示，此处只说明环节间计算口径。
             Δ 为站点币单件补亏额，C 为在线利润校正额（通过校验时为 0）。 -->
        <p v-if="item.decision?.stages" class="records-stage-formula">
          公式：① P（原实际营销价）→ ② P＋Δ（补亏损）→ ③ P＋Δ＋C（利润校验）
        </p>

        <dl class="records-log-prices records-log-uploads">
          <div><dt>一次毛利</dt><dd>{{ item.first_margin ? `${formatDecimal(item.first_margin.amount)} ${item.first_margin.currency}` : '未记录' }}</dd></div>
          <div><dt>建议上传价</dt><dd>{{ recordPrice(item.decision?.upload_price, item) }}</dd></div>
          <div><dt>上传含税价</dt><dd>{{ recordPrice(item.execution?.uploaded_price, item) }}</dd></div>
        </dl>
      </article>
      <p v-if="!visibleItems.length" class="records-log-empty" role="status">
        当前筛选没有匹配的日志，可换状态或清空关键词。
      </p>
    </div>
  </div>
</template>
