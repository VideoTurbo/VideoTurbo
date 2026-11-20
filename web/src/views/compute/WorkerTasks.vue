<script setup lang="ts">
// 一台机器上各 Worker 实例正在做的事，卡片和列表视图共用。
import { NButton } from 'naive-ui'
import { api, type Worker } from '@/api'
import ProgressBar from '@/components/ProgressBar.vue'
import TransferInfo from '@/components/TransferInfo.vue'
import WhisperjavInfo from '@/components/WhisperjavInfo.vue'
import { confirm, toast } from '@/lib/feedback'
import { duration, eta, fullDate } from '@/lib/format'
import { asrName } from '@/lib/jobs'
import type { Machine } from '@/lib/workers'
import { labelWorker, refreshStatus } from '@/stores/status'
import { t } from '@/i18n'

defineProps<{ machine: Machine }>()

/** 流水线中不占 GPU 的影片修复任务：预取下载、等待 GPU、后台回传。 */
function backgroundJobs(worker: Worker) {
  return (worker.restore_jobs || []).flatMap((job) => {
    if (job.job_id === worker.current_job_id || !job.progress) return []
    const kind = job.progress.stage === 'uploading' ? t('compute.tasks.uploading') : job.progress.stage === 'queued' ? t('compute.tasks.prefetched') : t('compute.tasks.prefetching')
    return [{ id: job.job_id, kind, progress: job.progress }]
  })
}

async function deleteWorker(name: string): Promise<void> {
  const label = labelWorker(name)
  if (!await confirm(t('compute.tasks.deleteConfirm', { label }), { title: t('compute.tasks.deleteTitle'), confirmLabel: t('common.delete'), danger: true })) return
  try {
    await api.deleteWorker(name)
    toast.ok(t('compute.tasks.deleted', { label }))
    await refreshStatus()
  } catch (error) {
    toast.error(t('compute.tasks.deleteFailed'), error)
  }
}
</script>

<template>
  <div v-for="{ worker, online, label } in machine.members" :key="worker.name" class="worker-task">
    <div v-if="!online" class="worker-task-head">
      <span><span v-if="label" class="worker-task-label">{{ label }}</span><span class="worker-idle">{{ t('compute.tasks.offline', { time: fullDate(worker.last_seen_at) }) }}</span></span>
      <NButton size="small" type="error" secondary @click="deleteWorker(worker.name)">{{ t('common.delete') }}</NButton>
    </div>
    <template v-else>
      <span v-if="label" class="worker-task-label">{{ label }}</span>
      <template v-if="worker.task_kind === 'restore' && worker.current_job_id">
        <TransferInfo v-if="worker.restore_progress" :progress="worker.restore_progress" />
        <b v-else>{{ t('restore.title') }}</b>
      </template>
      <template v-else-if="worker.current_job_id">
        <WhisperjavInfo v-if="worker.asr_mode === 'whisperjav' && worker.asr_whisperjav_progress" :progress="worker.asr_whisperjav_progress" :elapsed="worker.asr_elapsed_seconds" />
        <template v-else-if="worker.asr_total_chunks">
          <div class="info-head"><b>{{ t('compute.tasks.asrChunks', { completed: worker.asr_completed_chunks || 0, total: worker.asr_total_chunks, percent: ((100 * (worker.asr_completed_chunks || 0)) / worker.asr_total_chunks).toFixed(1) }) }}</b></div>
          <ProgressBar :percent="(100 * (worker.asr_completed_chunks || 0)) / worker.asr_total_chunks" />
          <div class="info-sub"><span>{{ t('compute.tasks.remaining', { time: eta(worker.asr_eta_seconds) }) }}</span></div>
        </template>
        <template v-else>
          <div class="info-head"><b>{{ t('compute.tasks.asrStage', { stage: worker.asr_stage || t('compute.tasks.preparing') }) }}</b></div>
          <div class="info-sub"><span>{{ t('compute.tasks.asrElapsed', { mode: asrName(worker.asr_mode), time: duration(worker.asr_elapsed_seconds) }) }}</span></div>
        </template>
      </template>
      <span v-else class="worker-idle">{{ backgroundJobs(worker).length ? t('compute.tasks.gpuIdleUploading') : t('compute.tasks.idle') }}</span>
      <div v-for="job in backgroundJobs(worker)" :key="job.id" class="worker-background">
        <span class="worker-background-kind">{{ job.kind }}</span>
        <TransferInfo :progress="job.progress" />
      </div>
    </template>
  </div>
</template>
