<script setup lang="ts">
// 桌面版：本机识别在识别程序连上之前的进度（等待下载、安装中、下载模型、出错）。
// 识别程序启动并报到后，本机和其他机器一样出现在列表里，这张卡片就不再显示。
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
const progress = computed(() => {
  const { index, total } = props.worker
  if (!installing.value || !index || !total) return null
  return { index, total, percent: Math.round(((index - 1) / total) * 100), step: locale === 'en' ? props.worker.en : props.worker.zh }
})

const state = computed(() => {
  switch (phase.value) {
    case 'pending': return { tone: 'warn', text: t('compute.local.pending') }
    case 'waiting': return { tone: 'work', text: t('compute.local.waiting') }
    case 'installing': return { tone: 'work', text: progress.value ? t('compute.local.installingStep', { index: progress.value.index, total: progress.value.total }) : t('compute.local.installing') }
    case 'self_test': return { tone: 'work', text: t('compute.local.selfTest') }
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
    case 'self_test': return t('compute.local.selfTestHint')
    case 'conflict': return t('compute.local.conflictHint')
    case 'need_tools': return t('compute.local.needToolsHint')
    case 'failed': return props.worker.message || t('compute.local.failedHint')
    default: return ''
  }
})

/** 等待下载时“开始下载”，出错时“重试”；需要命令行工具时只能在托盘窗口里安装。 */
const action = computed(() => (phase.value === 'pending' ? t('compute.local.download') : phase.value === 'failed' ? t('compute.local.retry') : ''))
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
      <div class="local-step"><span>{{ progress.step }}</span><span>{{ progress.percent }}%</span></div>
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
