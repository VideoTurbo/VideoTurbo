// 字幕任务的展示文字与操作。状态和可用操作由后端给出。
import type { Job, TranslationStats } from '@/api/types'
import { t } from '@/i18n'

export const asrName = (mode: string | undefined) => (mode === 'faster_whisper' ? 'Faster-Whisper' : mode === 'whisperjav' ? 'Qwen3-ASR' : mode || '—')

// ---- 排序：点同一列切换升降序；换列时影片名默认升序，大小和更新时间默认降序 ----
export type SortColumn = 'name' | 'size' | 'updated'
export type Sort = `${SortColumn}_${'asc' | 'desc'}`
export const isSort = (value: string | null): value is Sort => /^(updated|name|size)_(asc|desc)$/.test(value || '')

export function nextSort(current: Sort, column: SortColumn): Sort {
  const [currentColumn, direction] = current.split('_')
  if (currentColumn === column) return `${column}_${direction === 'asc' ? 'desc' : 'asc'}`
  return `${column}_${column === 'name' ? 'asc' : 'desc'}`
}

// ---- 重新处理 ----
export type ReprocessMode = 'continue' | 'retranslate' | 'reasr' | 'full'
export const reprocessNames: Record<ReprocessMode, string> = {
  continue: t('jobs.reprocess.continue'), retranslate: t('jobs.reprocess.retranslate'), reasr: t('jobs.reprocess.reasr'), full: t('jobs.reprocess.full'),
}

/** 行内最常用的一个操作。 */
export type PrimaryJobAction = 'start' | 'resume' | 'retry' | 'pause' | 'download' | null
export function primaryJobAction(job: Pick<Job, 'actions'>): PrimaryJobAction {
  const order: NonNullable<PrimaryJobAction>[] = ['start', 'resume', 'retry', 'pause', 'download']
  return order.find((action) => job.actions.includes(action)) ?? null
}

// ---- 翻译来源统计 ----
const providerNames: [string, string, string][] = [
  ['grok', 'Grok', 'Grok'], ['deepseek', 'DeepSeek', 'DeepSeek'], ['gemini', 'Gemini', 'Gemini'],
  ['other', t('jobs.model.other'), t('jobs.model.otherShort')], ['unknown', t('jobs.source.unknown'), t('jobs.source.unknownShort')],
]
const upstreamName = (key: string) =>
  key === 'fallback' ? t('jobs.model.deepseekFallback') : key === 'primary' ? t('jobs.model.primary') : key === 'unknown' ? t('jobs.source.unknown') : key
const positive = (counts: Record<string, number> = {}) => Object.keys(counts).filter((key) => Number(counts[key]) > 0)

export function modelMix(stats: TranslationStats | null | undefined): string {
  if (!stats) return ''
  const counts = stats.provider_cue_counts || {}, percentages = stats.provider_percentages || {}
  const parts = providerNames.filter(([key]) => Number(counts[key]) > 0)
    .map(([key, label]) => t('jobs.model.share', { label, percent: Number(percentages[key] || 0).toFixed(1), count: Number(counts[key]) }))
  return parts.length ? t('jobs.model.shares', { list: parts.join(' · ') }) : ''
}

/** 行内的简短占比，如 “Grok 82% · DeepSeek 18%”。 */
export function compactModelMix(stats: TranslationStats | null | undefined): string {
  if (!stats) return ''
  const counts = stats.provider_cue_counts || {}, percentages = stats.provider_percentages || {}
  return providerNames.filter(([key]) => Number(counts[key]) > 0)
    .map(([key, , short]) => `${short} ${Math.round(Number(percentages[key] || 0))}%`).join(' · ')
}

export function upstreamMix(stats: TranslationStats | null | undefined): string {
  if (!stats) return ''
  const counts = stats.upstream_cue_counts || {}, percentages = stats.upstream_percentages || {}
  const parts = positive(counts).map((key) => t('jobs.source.share', { name: upstreamName(key), percent: Number(percentages[key] || 0).toFixed(1), count: Number(counts[key]) }))
  return parts.length ? t('jobs.source.shares', { list: parts.join(' · ') }) : ''
}

export function upstreamFailures(stats: TranslationStats | null | undefined): string {
  if (!stats) return ''
  const counts = stats.upstream_failure_counts || {}
  const parts = positive(counts).map((key) => t('jobs.source.failures', { name: upstreamName(key), count: Number(counts[key]) }))
  return parts.length ? t('jobs.source.failureList', { list: parts.join(' · ') }) : ''
}

export const translationDetails = (stats: TranslationStats | null | undefined) =>
  [modelMix(stats), upstreamMix(stats), upstreamFailures(stats)].filter(Boolean)
