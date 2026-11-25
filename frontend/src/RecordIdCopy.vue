<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { CopyDocument } from '@element-plus/icons-vue'
import { copyRecordId } from './record-id-copy'

// 图标是展示选项；关闭后仍使用同一个原生按钮，保留键盘、读屏与复制反馈。
const props = withDefaults(defineProps<{ value: string; label: string; prefix?: string; showIcon?: boolean }>(), { showIcon: true })
const copying = ref(false)
async function copy() {
  copying.value = true
  try {
    ElMessage(await copyRecordId(props.value, props.label, document))
  } finally {
    copying.value = false
  }
}
</script>

<template>
  <span class="records-id-copy" :class="{ 'records-id-copy-text-only': !showIcon }">
    <button type="button" class="records-id-copy-button" :disabled="copying"
      :aria-label="`复制${label}：${value}`" :title="`点击复制${label}`" @click="copy">
      <span>{{ prefix }}{{ value }}</span><CopyDocument v-if="showIcon" aria-hidden="true" />
    </button>
  </span>
</template>
