<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { CopyDocument } from '@element-plus/icons-vue'
import { copyRecordId } from './record-id-copy'

const props = defineProps<{ value: string; label: string; prefix?: string }>()
const copying = ref(false)
async function copy() {
  copying.value = true
  try {
    ElMessage(await copyRecordId(props.value, props.label, navigator.clipboard))
  } finally {
    copying.value = false
  }
}
</script>

<template>
  <span class="records-id-copy">
    <button type="button" class="records-id-copy-button" :disabled="copying"
      :aria-label="`复制${label}：${value}`" :title="`点击复制${label}`" @click="copy">
      <span>{{ prefix }}{{ value }}</span><CopyDocument aria-hidden="true" />
    </button>
  </span>
</template>
