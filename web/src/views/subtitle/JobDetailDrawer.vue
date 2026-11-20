<script setup lang="ts">
// 字幕任务详情：结果、处理概要；中间文件收在“排查文件”里。打开时读取一次。
import { NButton } from 'naive-ui'
import { computed, ref, watch } from 'vue'
import { api, errorMessage, type JobDetail } from '@/api'
import DetailDrawer, { type DetailSection } from '@/components/DetailDrawer.vue'
import { fileName, fullDate, sizeText } from '@/lib/format'
import { asrName, compactModelMix, upstreamFailures } from '@/lib/jobs'
import { labelWorker } from '@/stores/status'
import { t } from '@/i18n'

/** 要查看的任务 ID；为 null 时关闭。 */
const jobId = defineModel<string | null>({ required: true })

const job = ref<JobDetail | null>(null)
const error = ref('')

watch(jobId, async (id) => {
  job.value = null
  error.value = ''
  if (!id) return
  try {
    const detail = await api.job(id)
    if (jobId.value === id) job.value = detail
  } catch (cause) {
    error.value = errorMessage(cause)
  }
})

const sections = computed<DetailSection[]>(() => {
  const j = job.value
  if (!j) return []
  const meta = j.asr_metadata
  const stats = j.translation_stats || j.translation_progress
  const models = meta?.passes?.map((pass) => pass.model).join(' + ') || meta?.model
  const passes = meta?.asr_engine === 'whisperjav' && meta.ensemble ? t('jobs.detail.dualPass') : ''
  const download = j.actions.includes('download')
  return [
    {
      title: t('jobs.detail.result'),
      rows: [{
        label: t('jobs.detail.subtitle'),
        value: j.status === 'completed' && j.subtitle_path ? fileName(j.subtitle_path) : null,
        href: download ? api.jobFileUrl(j.id, 'subtitle') : undefined,
      }],
    },
    {
      title: t('jobs.detail.summary'),
      rows: [
        { label: t('jobs.detail.asr'), value: meta?.asr_engine ? [asrName(meta.asr_engine), models, passes].filter(Boolean).join(' · ') : null },
        { label: t('jobs.detail.translation'), value: compactModelMix(stats) },
        { label: t('jobs.detail.failures'), value: upstreamFailures(stats).replace(/^调用失败：/, '') },
        { label: t('jobs.detail.machine'), value: j.lease_owner ? labelWorker(j.lease_owner) : null },
        { label: t('jobs.detail.created'), value: fullDate(j.created_at) },
        { label: t('jobs.detail.updated'), value: fullDate(j.updated_at) },
      ],
    },
  ]
})

const files = computed(() => (job.value?.files || []).filter((file) => file.exists))
</script>

<template>
  <DetailDrawer
    :show="jobId !== null" :title="job ? fileName(job.media_path) : t('jobs.detail.title')" :loading="!job" :error="error"
    :status="job?.status" :step="job?.step" :path="job?.media_path" :note="job?.last_error" :sections="sections"
    :tags="job ? [job.library_name, `${job.source_language_label} → ${job.target_language_label}`, Number(job.priority) > 0 ? '高优先级' : ''].filter((tag): tag is string => !!tag) : []"
    @close="jobId = null"
  >
    <details v-if="job && files.length" class="detail-files">
      <summary>{{ t('jobs.detail.files', { count: files.length }) }}</summary>
      <div class="file-list">
        <div v-for="file in files" :key="file.kind" class="file-row">
          <div><b>{{ file.label }}</b><div class="path wrap">{{ file.path }}</div></div>
          <NButton size="small" tag="a" target="_blank" :href="api.jobFileUrl(job.id, file.kind)">{{ t('jobs.detail.view', { size: sizeText(file.size) }) }}</NButton>
        </div>
      </div>
    </details>
  </DetailDrawer>
</template>
