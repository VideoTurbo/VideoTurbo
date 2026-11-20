<script setup lang="ts">
// 影片修复详情：结果与处理概要。数据就是列表里这一行，随列表刷新。
import { computed } from 'vue'
import type { RestoreMedia } from '@/api/types'
import DetailDrawer, { type DetailSection } from '@/components/DetailDrawer.vue'
import { clock, fileName, fullDate } from '@/lib/format'
import { labelWorker } from '@/stores/status'
import { t } from '@/i18n'

const props = defineProps<{ media: RestoreMedia | null; models: Record<string, string> }>()
const emit = defineEmits<{ close: [] }>()

const sections = computed<DetailSection[]>(() => {
  const job = props.media?.job
  if (!job) return []
  const reserved = job.reserved_machine && job.reserved_until && new Date(job.reserved_until).getTime() > Date.now()
  return [
    {
      title: t('restore.detail.result'),
      rows: job.state === 'COMPLETED'
        ? [
            { label: t('restore.detail.output'), value: job.output_path },
            { label: t('restore.detail.backup'), value: job.original_purged_at ? t('restore.detail.purged') : job.backup_path },
          ]
        : [],
    },
    {
      title: t('restore.detail.summary'),
      rows: [
        { label: t('restore.detail.model'), value: (props.models[job.restoration_model] || job.restoration_model) + (job.ltx_large_canvas ? t('restore.detail.largeCanvas') : '') },
        { label: t('restore.detail.machine'), value: job.lease_owner ? labelWorker(job.lease_owner) : reserved ? t('restore.detail.reserved', { machine: job.reserved_machine, time: clock(job.reserved_until) }) : null },
        { label: t('restore.detail.attempts'), value: job.attempts ? t('restore.detail.attemptCount', { attempts: job.attempts }) : null },
        { label: t('restore.detail.updated'), value: fullDate(props.media?.updated_at) },
      ],
    },
  ]
})

const note = computed(() => props.media?.job?.last_error || props.media?.status_reason)
</script>

<template>
  <DetailDrawer
    :show="media !== null" :title="media ? fileName(media.path) : t('restore.detail.title')"
    :status="media?.status" :step="media?.step" :path="media?.path" :note="note" :sections="sections"
    :tags="media ? [media.library_name] : []"
    @close="emit('close')"
  />
</template>
