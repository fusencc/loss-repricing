<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { display, errorMessage, modeLabel, type Contracts, type Settings } from './api'

const props = defineProps<{
  settings: Settings
  contracts: Contracts
  liveEnabled: boolean
  save: (settings: Partial<Settings>) => Promise<void>
}>()
const sites = ['MY', 'PH', 'SG', 'TH', 'VN']
function copySettings(value: Settings): Settings {
  return {
    ...value,
    paused_sites: [...value.paused_sites],
    pilot_shops: [...value.pilot_shops],
    site_enabled: [...value.site_enabled],
  }
}

const form = ref(copySettings(props.settings))
const busy = ref(false)
const failure = ref('')
const dirty = computed(() => JSON.stringify(form.value) !== JSON.stringify(props.settings))
const cleanShops = computed(() => [...new Set(form.value.pilot_shops.map(shop => shop.trim()).filter(Boolean))])
const incompleteScope = computed(() => form.value.autonomous_enabled && (!form.value.site_enabled.length || !cleanShops.value.length))
const siteOptions = computed(() => [...new Set([...sites, ...form.value.site_enabled, ...form.value.paused_sites])])
watch(() => props.settings, (value) => { form.value = copySettings(value) })

async function save() {
  if (busy.value || !dirty.value || incompleteScope.value) return
  busy.value = true
  failure.value = ''
  try {
    const updated = { ...form.value, pilot_shops: cleanShops.value }
    const changed = Object.entries(updated).filter(([key, value]) =>
      JSON.stringify(value) !== JSON.stringify(props.settings[key as keyof Settings]),
    )
    if (changed.length) await props.save(Object.fromEntries(changed) as Partial<Settings>)
  } catch (error) {
    failure.value = errorMessage(error)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="settings-grid">
    <section class="panel settings-panel">
      <header class="panel-heading"><div><h2>调度与试点</h2><p class="muted">修改配置后，点击保存使配置生效。</p></div></header>
      <el-form label-position="top" :disabled="busy" @submit.prevent="save">
        <div class="setting-line">
          <div><strong id="paused-label">全局暂停</strong><p>暂停新提交，继续核验已提交结果。</p></div>
          <el-switch v-model="form.paused" aria-labelledby="paused-label" />
        </div>
        <div class="setting-line">
          <div><strong id="autonomous-label">开启自动运行</strong><p>自动运行仅覆盖试点店铺和已启用的站点。真实提交需要完成数据接入，并开启真实提交开关。</p></div>
          <el-switch v-model="form.autonomous_enabled" aria-labelledby="autonomous-label" />
        </div>
        <div class="setting-line">
          <div><strong id="schedule-label">开启定时检查</strong><p>北京时间 09:00 起，每 5 分钟检查一次。</p></div>
          <el-switch v-model="form.schedule_enabled" aria-labelledby="schedule-label" />
        </div>
        <div class="settings-fields">
          <el-form-item label="试点店铺 ID">
            <el-select v-model="form.pilot_shops" multiple filterable allow-create default-first-option placeholder="输入店铺 ID 后按回车">
              <el-option v-for="shop in settings.pilot_shops" :key="shop" :label="shop" :value="shop" />
            </el-select>
          </el-form-item>
          <el-form-item label="启用站点">
            <el-select v-model="form.site_enabled" multiple placeholder="选择已批准的试点站点">
              <el-option v-for="site in siteOptions" :key="site" :label="site" :value="site" />
            </el-select>
          </el-form-item>
          <el-form-item label="暂停站点">
            <el-select v-model="form.paused_sites" multiple placeholder="选择暂停新提交的站点">
              <el-option v-for="site in siteOptions" :key="site" :label="site" :value="site" />
            </el-select>
          </el-form-item>
        </div>
        <el-alert v-if="incompleteScope" title="开启自动运行前，请填写试点店铺并选择启用站点。" type="warning" show-icon :closable="false" />
        <el-alert v-if="failure" :title="failure" type="error" show-icon :closable="false" />
        <div class="form-actions">
          <el-button :disabled="!dirty" @click="form = copySettings(settings); failure = ''">恢复已保存配置</el-button>
          <el-button type="primary" native-type="submit" :disabled="!dirty || incompleteScope" :loading="busy">保存配置</el-button>
        </div>
      </el-form>
    </section>
    <section class="panel settings-panel">
      <header class="panel-heading"><h2>接口就绪状态</h2><span class="mode-pill" :class="`mode-${contracts.mode}`">{{ modeLabel(contracts.mode) }}</span></header>
      <el-alert v-if="contracts.mode === 'live'" title="数据源待接入" type="info" show-icon :closable="false" />
      <dl class="readiness-list">
        <div><dt>真实提交开关</dt><dd>{{ liveEnabled ? '已开启' : '未开启' }}</dd></div>
        <div><dt>已核验站点</dt><dd>{{ contracts.verified_sites.join('、') || '暂无' }}</dd></div>
      </dl>
      <div v-if="contracts.missing.length" class="contract-gaps">
        <h3>待补齐的接入信息</h3>
        <ul class="missing-list"><li v-for="entry in contracts.missing" :key="entry">{{ entry }}</li></ul>
      </div>
      <details class="evidence"><summary>展开来源版本</summary><pre>{{ display(contracts.source_versions) }}</pre></details>
    </section>
  </div>
</template>

