<script setup lang="ts">
import { computed, ref } from 'vue'
import { Plus } from '@element-plus/icons-vue'
import { errorMessage, timeLabel, type Protection } from './api'

type ProtectionInput = Pick<Protection, 'shop_id' | 'site' | 'main_sku' | 'reason'>
const props = defineProps<{
  protections: Protection[]
  loading: boolean
  add: (protection: ProtectionInput) => Promise<void>
  remove: (protection: Protection) => Promise<void>
}>()
const sites = ['MY', 'PH', 'SG', 'TH', 'VN']
const form = ref<ProtectionInput>({ shop_id: '', site: '', main_sku: '', reason: '' })
const filter = ref<'all' | 'active' | 'inactive'>('active')
const visible = computed(() => props.protections.filter(entry => filter.value === 'all' || entry.active === (filter.value === 'active')))
const open = ref(false)
const busy = ref(false)
const failure = ref('')
const formFailure = ref('')
const complete = computed(() => Object.values(form.value).every(value => value.trim().length > 0))

function openForm() {
  form.value = { shop_id: '', site: '', main_sku: '', reason: '' }
  formFailure.value = ''
  open.value = true
}

async function add() {
  if (busy.value || !complete.value) return
  busy.value = true
  formFailure.value = ''
  try {
    await props.add({
      shop_id: form.value.shop_id.trim(),
      site: form.value.site,
      main_sku: form.value.main_sku.trim(),
      reason: form.value.reason.trim(),
    })
    open.value = false
    filter.value = 'active'
  } catch (error) {
    formFailure.value = errorMessage(error)
  } finally {
    busy.value = false
  }
}

async function remove(entry: Protection) {
  if (busy.value) return
  busy.value = true
  failure.value = ''
  try {
    await props.remove(entry)
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') failure.value = errorMessage(error)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="panel table-panel">
    <div class="table-toolbar">
      <div><h2>人工保护记录</h2><p class="muted">启用中的保护项按店铺、站点和主 SKU 匹配。</p></div>
      <div class="toolbar-actions">
        <el-select v-model="filter" aria-label="保护状态">
          <el-option label="启用中" value="active" />
          <el-option label="已停用" value="inactive" />
          <el-option label="全部状态" value="all" />
        </el-select>
        <el-button type="primary" :icon="Plus" :disabled="busy" @click="openForm">新增保护</el-button>
      </div>
    </div>
    <el-alert v-if="failure" :title="failure" type="error" show-icon :closable="false" />
    <el-table v-loading="loading" :data="visible" row-key="id" empty-text="没有符合条件的保护记录">
      <el-table-column prop="shop_id" label="店铺 ID" min-width="125" />
      <el-table-column prop="site" label="站点" width="75" />
      <el-table-column prop="main_sku" label="主 SKU" min-width="135" />
      <el-table-column prop="reason" label="保护原因" min-width="165" show-overflow-tooltip />
      <el-table-column label="状态" width="86"><template #default="{ row }"><el-tag :type="row.active ? 'success' : 'info'" effect="light">{{ row.active ? '启用中' : '已停用' }}</el-tag></template></el-table-column>
      <el-table-column label="创建时间" min-width="165"><template #default="{ row }">{{ timeLabel(row.created_at) }}</template></el-table-column>
      <el-table-column label="操作" width="85"><template #default="{ row }"><el-button v-if="row.active" type="danger" link :disabled="busy" @click="remove(row)">停用</el-button></template></el-table-column>
    </el-table>
    <el-dialog v-model="open" title="新增人工保护" width="min(490px, calc(100vw - 32px))" :close-on-click-modal="!busy" :close-on-press-escape="!busy" :show-close="!busy">
      <el-form label-position="top" :disabled="busy" @submit.prevent="add">
        <el-form-item label="店铺 ID" required><el-input v-model="form.shop_id" placeholder="输入店铺 ID" /></el-form-item>
        <el-form-item label="站点" required>
          <el-select v-model="form.site" placeholder="选择站点"><el-option v-for="site in sites" :key="site" :label="site" :value="site" /></el-select>
        </el-form-item>
        <el-form-item label="主 SKU" required><el-input v-model="form.main_sku" placeholder="输入需要保护的主 SKU" /></el-form-item>
        <el-form-item label="保护原因" required><el-input v-model="form.reason" type="textarea" :rows="3" placeholder="记录保护依据" /></el-form-item>
        <el-alert v-if="formFailure" :title="formFailure" type="error" show-icon :closable="false" />
        <div class="form-actions">
          <el-button :disabled="busy" @click="open = false">取消</el-button>
          <el-button type="primary" native-type="submit" :disabled="!complete" :loading="busy">添加保护</el-button>
        </div>
      </el-form>
    </el-dialog>
  </section>
</template>
