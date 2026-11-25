<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { ElTooltip } from 'element-plus'
import { CopyDocument } from '@element-plus/icons-vue'
import { copyRecordId } from './record-id-copy'

const props = withDefaults(defineProps<{ value: string; label: string; prefix?: string; showIcon?: boolean; inline?: boolean }>(), { showIcon: true })
const feedback = ref('')
let dismiss: number | undefined

async function copy() {
  window.clearTimeout(dismiss)
  feedback.value = ''
  const result = await copyRecordId(props.value, props.label, document)
  feedback.value = result.message
  dismiss = window.setTimeout(() => { feedback.value = '' }, result.duration)
}

onBeforeUnmount(() => window.clearTimeout(dismiss))
</script>

<template>
  <span class="records-id-copy" :class="{ 'records-id-copy-text-only': !showIcon, 'records-id-copy--inline': inline }">
    <!-- 挂在遮罩内，避开列表滚动裁剪并保持在日历浮层下方。 -->
    <ElTooltip :visible="!!feedback" append-to=".records-overlay" placement="right-start"
      :fallback-placements="['top-start', 'bottom-start']" :offset="8"
      :show-arrow="false" :enterable="false" popper-class="records-id-copy-feedback">
      <button type="button" class="records-id-copy-button"
        :aria-label="`复制${label}：${value}`" :title="`点击复制${label}`" @click="copy">
        <span>{{ prefix }}{{ value }}</span><CopyDocument v-if="showIcon" aria-hidden="true" />
      </button>
      <template #content><span aria-hidden="true">{{ feedback }}</span></template>
    </ElTooltip>
    <span class="records-sr" role="status">{{ feedback }}</span>
  </span>
</template>

<style scoped>
.records-id-copy--inline {
  display: inline-block;
  max-width: 100%;
}

.records-id-copy--inline .records-id-copy-button {
  white-space: normal;
}

/* Element Plus 将提示挂在遮罩内，该浮层不会接收鼠标或键盘焦点。 */
:global(.records-id-copy-feedback.el-popper.is-dark) {
  max-width: min(260px, calc(100vw - 16px));
  padding: 6px 10px;
  border: 1px solid #dbe8ff33;
  border-radius: 8px;
  background: rgba(16, 24, 40, .94);
  color: #f3f7fc;
  font: 13px/1.5 "Segoe UI", "DengXian", "Microsoft YaHei", "PingFang SC", sans-serif;
  box-sizing: border-box;
  overflow-wrap: anywhere;
  pointer-events: none;
}

:global(.records-id-copy-feedback[data-popper-reference-hidden="true"]) {
  visibility: hidden;
}
</style>
