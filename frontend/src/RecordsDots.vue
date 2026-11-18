<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'

const patches = ref<{ id: number; style: Record<string, string> }[]>([])
const motion = window.matchMedia('(prefers-reduced-motion: reduce)')
const random = (min: number, max: number) => min + Math.random() * (max - min)
const anchors = [[16, 62], [42, 84], [68, 61], [85, 83], [86, 27], [18, 34]] as const
let timer: number | undefined
let id = 0

function show() {
  const count = Math.floor(random(1, 4))
  const available = [...anchors]
  let end = 0
  patches.value = Array.from({ length: count }, () => {
    // 每轮不重复选区，区内再随机偏移；各簇独立决定形状和出现时刻。
    const [x, y] = available.splice(Math.floor(random(0, available.length)), 1)[0]!
    const duration = random(6200, 9800)
    const delay = random(0, 1000)
    end = Math.max(end, duration + delay)
    return { id: ++id, style: {
      left: `${x + random(-6, 6)}%`, top: `${y + random(-5, 5)}%`,
      width: `min(${random(375, 765)}px, 96vw)`, height: `min(${random(225, 525)}px, 72vh)`,
      '--dot-angle': `${random(-65, 65)}deg`, '--dot-peak': `${random(.18, .29)}`,
      animationDuration: `${duration}ms`, animationDelay: `${delay}ms`,
    } }
  })
  timer = window.setTimeout(show, end + random(550, 2900))
}

function syncMotion() {
  window.clearTimeout(timer)
  patches.value = []
  if (!motion.matches) timer = window.setTimeout(show, random(300, 1300))
}

onMounted(() => { motion.addEventListener('change', syncMotion); syncMotion() })
onUnmounted(() => { motion.removeEventListener('change', syncMotion); window.clearTimeout(timer) })
</script>

<template>
  <div class="records-dots" aria-hidden="true">
    <span v-for="patch in patches" :key="patch.id" :style="patch.style"></span>
  </div>
</template>
