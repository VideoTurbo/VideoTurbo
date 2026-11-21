<script setup lang="ts">
// 桌面版：本机识别在识别程序连上之前的进度（等待下载、安装中、下载模型、出错）。
// 识别程序启动并报到后，本机和其他机器一样出现在列表里，这张卡片就不再显示；
// 只有控制台换了识别模式、本机缺新模式的模型时，才再显示出来提示下载。
import { NButton } from 'naive-ui'
import { computed, ref } from 'vue'
import { api, errorMessage, type LocalWorker } from '@/api'
import { toast } from '@/lib/feedback'
import { refreshStatus } from '@/stores/status'
import { locale, t } from '@/i18n'

const props = defineProps<{ worker: LocalWorker }>()
const starting = ref(false)

const phase = computed(() => props.worker.phase)
const installing = computed(() => phase.value === 'installing')
const missing = computed(() => (phase.value === 'running' ? props.worker.missing || [] : []))
const missingSize = computed(() => size(missing.value.reduce((sum, model) => sum + (model.size_mb || 0), 0)))

function size(mb: number): string {
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`
}

const progress = computed(() => {
  const { index, total, done_mb: done, total_mb: all } = props.worker
  const step = locale === 'en' ? props.worker.en : props.worker.zh
  if (phase.value === 'models' && all) {
    return { step: `${index}/${total} ${step}`, detail: `${size(done || 0)} / ${size(all)}`, percent: Math.min(100, Math.round(((done || 0) / all) * 100)) }
  }
  if (!installing.value || !index || !total) return null
  return { step, detail: `${Math.round(((index - 1) / total) * 100)}%`, percent: Math.round(((index - 1) / total) * 100) }
})

const state = computed(() => {
  switch (phase.value) {
    case 'pending': return { tone: 'warn', text: t('compute.local.pending') }
    case 'waiting': return { tone: 'work', text: t('compute.local.waiting') }
    case 'installing': return { tone: 'work', text: props.worker.index && props.worker.total ? t('compute.local.installingStep', { index: props.worker.index, total: props.worker.total }) : t('compute.local.installing') }
    case 'models': return { tone: 'work', text: t('compute.local.models') }
    case 'self_test': return { tone: 'work', text: t('compute.local.selfTest') }
    case 'running': return { tone: 'warn', text: t('compute.local.missingState') }
    case 'conflict': return { tone: 'plain', text: t('compute.local.conflict') }
    case 'need_tools': return { tone: 'warn', text: t('compute.local.needTools') }
    case 'failed': return { tone: 'bad', text: t('compute.local.failed') }
    default: return { tone: 'work', text: phase.value }
  }
})

const hint = computed(() => {
  switch (phase.value) {
    case 'pending': return t('compute.local.pendingHint')
    case 'waiting': return t('compute.local.waitingHint')
    case 'installing': return t('compute.local.installingHint')
    case 'models': return t('compute.local.modelsHint')
    case 'self_test': return t('compute.local.selfTestHint')
    case 'running': return t('compute.local.missingHint', { names: missing.value.map((m) => `${locale === 'en' ? m.en : m.zh}（${size(m.size_mb || 0)}）`).join(locale === 'en' ? ', ' : '、') })
    case 'conflict': return t('compute.local.conflictHint')
    case 'need_tools': return t('compute.local.needToolsHint')
    case 'failed': return props.worker.message || t('compute.local.failedHint')
    default: return ''
  }
})

/** 等待下载时“开始下载”，出错时“重试”；需要命令行工具时只能在托盘窗口里安装。 */
const action = computed(() => {
  if (phase.value === 'pending') return t('compute.local.download')
  if (phase.value === 'failed') return t('compute.local.retry')
  if (missing.value.length) return t('compute.local.downloadSize', { size: missingSize.value })
  return ''
})
const requested = computed(() => starting.value || props.worker.download_requested)

async function start(): Promise<void> {
  starting.value = true
  try {
    await api.downloadLocalWorker()
    await refreshStatus()
  } catch (error) {
    toast.fail(errorMessage(error))
  } finally {
    starting.value = false
  }
}
</script>

<template>
  <div class="worker local-worker">
    <div class="worker-top">
      <div>
        <span class="worker-name">{{ t('compute.local.title') }}</span>
        <div class="worker-gpu">{{ t('compute.local.sub') }}</div>
      </div>
      <span class="pill" :class="state.tone">{{ state.text }}</span>
    </div>
    <div v-if="progress" class="local-progress">
      <div class="local-step"><span>{{ progress.step }}</span><span>{{ progress.detail }}</span></div>
      <div class="meter"><span :style="{ width: `${progress.percent}%` }" /></div>
    </div>
    <p class="local-hint" :class="{ 'warn-text': phase === 'failed' }">{{ hint }}</p>
    <p v-if="worker.line && (installing || phase === 'self_test')" class="local-line" :title="worker.line">{{ worker.line }}</p>
    <div v-if="action" class="local-actions">
      <span class="local-tray">{{ t('compute.local.trayHint') }}</span>
      <NButton size="small" type="primary" :loading="requested" :disabled="requested" @click="start">{{ action }}</NButton>
    </div>
    <div v-else class="local-tray">{{ t('compute.local.trayHint') }}</div>
  </div>
</template>

<style scoped>
.local-worker { margin-bottom: 12px; }
.local-progress { font-size: 12px; }
.local-step { display: flex; justify-content: space-between; gap: 8px; color: var(--text-2); }
.local-hint { margin: 0; font-size: 13px; line-height: 1.55; overflow-wrap: anywhere; }
.local-line { margin: 0; font-size: 12px; color: var(--text-3); font-family: var(--mono, ui-monospace, monospace); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.local-actions { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.local-tray { font-size: 12px; color: var(--text-3); }
</style>
