<script setup lang="ts">
// 影片修复列表的状态列：一部影片最近一次修复任务的状态与进度。
import { computed } from 'vue'
import type { RestoreMedia } from '@/api/types'
import ProgressBar from '@/components/ProgressBar.vue'
import TransferInfo from '@/components/TransferInfo.vue'
import { clock, fileName, sizeText } from '@/lib/format'
import { isLtx } from '@/lib/restore'
import { statusLabels, statusTones } from '@/lib/status'
import { labelWorker } from '@/stores/status'
import { t } from '@/i18n'

const props = defineProps<{ media: RestoreMedia; models: Record<string, string> }>()

const job = computed(() => props.media.job)
/** 排队、处理中、失败和修复完成时显示任务的模型和进度；待处理和不需要修复时只显示说明。 */
const showJob = computed(() => !!job.value && ['queued', 'running', 'failed'].includes(props.media.status)
  || (props.media.status === 'completed' && job.value?.state === 'COMPLETED'))

const local = computed(() => {
  const progress = job.value?.local_progress
  if (!progress?.total_bytes) return null
  return {
    title: progress.stage === 'hashing' ? t('restore.status.checkSource') : t('restore.status.checkUpload'),
    percent: (100 * (progress.done_bytes || 0)) / progress.total_bytes,
    text: `${sizeText(progress.done_bytes)} / ${sizeText(progress.total_bytes)}`,
  }
})

const waitText = computed(() => {
  const j = job.value
  if (!j || j.state !== 'WAITING') return ''
  const reserved = j.reserved_machine && j.reserved_until && new Date(j.reserved_until).getTime() > Date.now()
  const text = reserved
    ? t('restore.status.reserved', { machine: j.reserved_machine, time: clock(j.reserved_until) })
    : t('restore.status.waiting', { model: isLtx(j.restoration_model) ? 'LTX' : 'BasicVSR++' })
  return text + (j.attempts ? t('restore.status.attempts', { attempts: j.attempts }) : '')
})
</script>

<template>
  <span class="pill" :class="statusTones[media.status]">{{ statusLabels[media.status] }}</span>
  <template v-if="showJob && job">
    <span class="state-note">{{ media.step ? `${media.step} · ` : '' }}{{ models[job.restoration_model] || job.restoration_model }}{{ job.ltx_large_canvas ? t('restore.status.largeCanvas') : '' }}</span>
    <template v-if="job.progress">
      <div v-if="job.lease_owner" class="dc-sub"><span class="inline-worker" :title="job.lease_owner">{{ labelWorker(job.lease_owner) }}</span></div>
      <TransferInfo :progress="job.progress" />
    </template>
    <template v-else-if="local">
      <div class="info-head"><b>{{ local.title }}</b><span class="num">{{ local.percent.toFixed(1) }}%</span></div>
      <ProgressBar :percent="local.percent" />
      <div class="dc-sub">{{ local.text }}</div>
    </template>
    <div v-else-if="job.local_progress?.detail" class="info-head"><b>{{ job.local_progress.detail }}</b></div>
    <template v-else-if="job.state === 'COMPLETED'">
      <div v-if="job.output_path" class="dc-sub" :title="job.output_path">{{ t('restore.status.output', { name: fileName(job.output_path) }) }}</div>
      <div v-if="job.original_purged_at" class="dc-sub">{{ t('restore.status.purged') }}</div>
      <div v-else-if="job.backup_path" class="dc-sub" :title="job.backup_path">{{ t('restore.status.backup', { path: job.backup_path }) }}</div>
    </template>
    <div v-else-if="waitText" class="dc-sub" :title="waitText">{{ waitText }}</div>
    <div v-if="job.last_error" class="job-error" :title="job.last_error">{{ job.last_error }}</div>
  </template>
  <div v-else-if="media.status_reason" class="dc-sub" :title="media.status_reason">{{ media.status_reason }}</div>
</template>
