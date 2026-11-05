<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, errorMessage, type Ownership } from './api'
const scopes = ref<Ownership[]>([]),
  error = ref('')
onMounted(async () => {
  try {
    scopes.value = (await api.get<Ownership[]>('/scopes')).data
  } catch (e) {
    error.value = errorMessage(e)
  }
})
</script>
<template>
  <section class="panel table-panel">
    <div class="table-toolbar">
      <div>
        <h2>我的负责范围</h2>
        <p class="muted">
          商品、批次与通知按服务器当前归属授权。转交后旧页面操作会被拒绝。
        </p>
      </div>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-table :data="scopes" empty-text="尚未分配负责范围，请联系管理员">
      <el-table-column prop="platform" label="平台" />
      <el-table-column prop="shop_id" label="店铺" />
      <el-table-column prop="site" label="站点" />
      <el-table-column label="目标范围">
        <template #default="{ row }">
          {{ row.target_id || '整个店铺站点' }}
        </template>
      </el-table-column>
      <el-table-column label="自动托管">
        <template #default="{ row }">
          {{ row.autonomous ? '已授权' : '未授权' }}
        </template>
      </el-table-column>
    </el-table>
  </section>
</template>
