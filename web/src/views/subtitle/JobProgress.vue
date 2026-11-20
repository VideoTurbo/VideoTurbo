<script setup lang="ts">
// 任务行里的进度：语音识别（两种后端）或翻译。
import { NTooltip } from 'naive-ui'
import { computed } from 'vue'
import type { Job } from '@/api/types'
import AppIcon from '@/components/AppIcon.vue'
import ProgressBar from '@/components/ProgressBar.vue'
import WhisperjavInfo from '@/components/WhisperjavInfo.vue'
import { clampPercent, duration, eta } from '@/lib/format'
import { asrName, compactModelMix, translationDetails } from '@/lib/jobs'
import { labelWorker } from '@/stores/status'
import { t } from '@/i18n'

const props = defineProps<{ job: Job }>()

const asr = computed(() => props.job.asr_progress)
const translation = computed(() => props.job.translation_progress)
const details = computed(() => translationDetails(props.job.translation_stats || translation.value))

const translationTip = computed(() => {
  const p = translation.value
  if (!p) return details.value.join('\n')
  const tokens = (Number(p.prompt_tokens) || 0) + (Number(p.completion_tokens) || 0)
  return [
    t('jobs.progress.requests', { inflight: p.inflight_requests, limit: p.max_concurrency, seconds: p.average_request_seconds || 0 }),
    t('jobs.progress.usage', { tokens, requests: p.request_count, fallback: p.fallback_count || 0, retries: p.retry_count, splits: p.split_count }),
    t('jobs.progress.tokenNote'),
    ...details.value,
  ].join('\n')
})

// 翻译进行中时显示翻译自己的错误，其余情况显示任务的错误。
const error = computed(() => (!asr.value && translation.value ? translation.value.last_error : props.job.last_error))
</script>

<template>
  <WhisperjavInfo v-if="asr?.mode === 'whisperjav'" :progress="asr" :elapsed="asr.elapsed_seconds" />
  <template v-else-if="asr?.stage && !asr.total_chunks">
    <div class="info-head"><b>{{ asrName(asr.mode) }} · {{ asr.stage }}</b></div>
    <div class="info-sub"><span>{{ t('jobs.progress.elapsed', { time: duration(asr.elapsed_seconds) }) }}</span></div>
  </template>
  <template v-else-if="asr">
    <div class="info-head">
      <b>
        <span v-if="job.lease_owner" class="inline-worker" :title="job.lease_owner">{{ labelWorker(job.lease_owner) }} · </span>
        {{ t('jobs.progress.chunks', { completed: asr.completed_chunks, total: asr.total_chunks, percent: clampPercent(asr.percent).toFixed(1) }) }}
      </b>
    </div>
    <ProgressBar :percent="asr.percent" />
    <div class="info-sub"><span>{{ t('jobs.progress.elapsedRemaining', { elapsed: duration(asr.elapsed_seconds), remaining: eta(asr.eta_seconds) }) }}</span></div>
  </template>
  <template v-else-if="translation">
    <div class="info-head">
      <b>{{ t('jobs.progress.translated', { completed: translation.completed_cues, total: translation.total_cues }) }}</b>
      <span class="num">{{ clampPercent(translation.progress_percent).toFixed(1) }}%</span>
    </div>
    <ProgressBar :percent="translation.progress_percent" />
    <div class="info-sub">
      <span>{{ t('jobs.progress.translationLive', { remaining: eta(translation.eta_seconds), inflight: translation.inflight_requests, limit: translation.max_concurrency }) }}{{ translation.fallback_count ? t('jobs.progress.fallback', { count: translation.fallback_count }) : '' }}</span>
      <NTooltip>
        <template #trigger><span class="info-tip" tabindex="0"><AppIcon name="info" /></span></template>
        <div class="tip-text">{{ translationTip }}</div>
      </NTooltip>
    </div>
  </template>
  <div v-else-if="details.length" class="info-sub">
    <span>{{ compactModelMix(job.translation_stats) || details[0] }}</span>
    <NTooltip>
      <template #trigger><span class="info-tip" tabindex="0"><AppIcon name="info" /></span></template>
      <div class="tip-text">{{ translationTip }}</div>
    </NTooltip>
  </div>
  <div v-if="error" class="job-error" :class="{ note: job.status === 'completed' || job.status === 'paused' }" :title="error">{{ error }}</div>
</template>
