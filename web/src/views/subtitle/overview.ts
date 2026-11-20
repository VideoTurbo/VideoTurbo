// 字幕页顶部的概览：最近扫描、提示条、翻译实况。全部由总状态推导。
import { computed } from 'vue'
import { api, type Warning } from '@/api'
import { toast } from '@/lib/feedback'
import { shortDate } from '@/lib/format'
import { asrName } from '@/lib/jobs'
import { supportsAsr, workerOnline } from '@/lib/workers'
import { heartbeatTimeout, refreshStatus, status } from '@/stores/status'
import { t } from '@/i18n'

export const pipelinePaused = computed(() => status.value?.pipeline_paused === true)

export const scanSummary = computed(() => {
  const scan = status.value?.last_scan
  if (!scan?.started_at) return status.value ? t('overview.scan.none') : ''
  return t('overview.scan.last', { time: shortDate(scan.started_at), scanned: scan.scanned || 0, pending: scan.pending || 0 })
    + t('overview.scan.lastMore', { completed: scan.completed || 0, unusable: scan.unusable || 0 })
    + (scan.unsettled ? t('overview.scan.unsettled', { unsettled: scan.unsettled }) : '')
    + (scan.relocated ? t('overview.scan.relocated', { relocated: scan.relocated }) : '')
})

export const warnings = computed(() => {
  const data = status.value
  if (!data) return []
  const list: Warning[] = []
  if (data.translation?.primary_available === false) list.push({ message: t('overview.translation.primaryDown'), target: 'translate' })
  const hasAsrWorker = data.workers.some((worker) => supportsAsr(worker, data.asr) && workerOnline(worker, heartbeatTimeout.value))
  if (!hasAsrWorker) {
    const mode = data.asr?.mode || 'whisperjav'
    const ensemble = mode === 'whisperjav' && data.asr?.whisperjav_ensemble ? t('overview.asr.dualPass') : ''
    list.push({ message: t('overview.asr.noMachine', { ensemble, mode: asrName(mode) }), target: 'compute' })
  }
  list.push(...(data.warnings || []))
  const errors = data.last_scan?.errors?.length
  if (errors) list.push({ message: t('overview.scan.errors', { errors }) })
  return list
})

export const translationLive = computed(() => {
  const tr = status.value?.translation
  if (!tr || !(tr.active_jobs > 0)) return ''
  const all = [...(tr.upstreams || []), ...(tr.fallback_upstreams || [])]
  const ready = all.filter((item) => item.configured && item.enabled)
  const sum = (key: 'active_requests' | 'concurrency') => ready.reduce((total, item) => total + (Number(item[key]) || 0), 0)
  const cooling = ready.filter((item) => item.cooldown_seconds > 0).map((item) => item.name)
  const nodes = all.length
    ? t('overview.translation.nodes', { ready: ready.length, total: all.length, active: sum('active_requests'), limit: sum('concurrency') })
      + (cooling.length ? t('overview.translation.cooling', { names: cooling.join(t('common.listSeparator')) }) : '')
    : ''
  return t('overview.translation.live', {
    active: tr.active_jobs, jobs: tr.job_concurrency, inflight: tr.inflight_requests, limit: tr.request_concurrency, requests: tr.request_count || 0,
  }) + t('overview.translation.counts', { fallback: tr.fallback_count || 0, retries: tr.retry_count || 0 }) + nodes
})

/** 界面不提供暂停流水线的入口；流水线处于暂停时，提示条上可以继续。 */
export async function resumePipeline(): Promise<void> {
  try {
    await api.resumePipeline()
    toast.ok(t('overview.resumed'))
    await refreshStatus()
  } catch (error) {
    toast.error(t('common.actionFailed'), error)
  }
}
