<script setup lang="ts">
import { ref } from 'vue'
import { api, errorMessage, timeLabel } from './api'
interface RateRow { currency: string | null; source_rate: string | null; units_per_cny: string | null; error: string | null }
interface RateResult { queried_at: string; effective_date: string | null; message: string; rows: RateRow[] }
const busy = ref(false)
const failure = ref('')
const result = ref<RateResult | null>(null)
async function query() {
  if (busy.value) return
  busy.value = true
  failure.value = ''
  result.value = null
  try { result.value = (await api.get<RateResult>('/sources/erp/rates', { timeout: 70000 })).data }
  catch (error) { failure.value = errorMessage(error) }
  finally { busy.value = false }
}
</script>
<template>
  <section class="panel settings-panel">
    <header class="panel-heading"><h2>公司汇率查询</h2><el-button :loading="busy" @click="query">查询公司当前汇率</el-button></header>
    <p class="muted">点击后访问公司 ERP 真实只读接口。显示系统当前配置值，查询时间不代表汇率生效日期。</p>
    <el-alert v-if="failure" :title="failure" type="error" :closable="false" show-icon />
    <template v-if="result">
      <el-alert :title="result.message" type="warning" :closable="false" show-icon />
      <p>查询时间：{{ timeLabel(result.queried_at) }} · 生效日期：{{ result.effective_date || '接口未提供' }}</p>
      <el-table :data="result.rows" max-height="480" empty-text="接口本次未返回汇率">
        <el-table-column prop="currency" label="来源币种" min-width="95" />
        <el-table-column prop="source_rate" label="接口原始汇率" min-width="135" />
        <el-table-column prop="units_per_cny" label="每 1 CNY 对应外币（推断参考）" min-width="230" show-overflow-tooltip />
        <el-table-column prop="error" label="异常说明" min-width="180" />
      </el-table>
      <p class="muted">原始汇率按“每单位外币折合人民币”推断转换，待公司确认。参考值由后端计算，暂未进入自动改价。</p>
    </template>
  </section>
</template>
