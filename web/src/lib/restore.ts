// 影片修复的展示文字。状态和可用操作由后端给出。
import type { RestoreAction, TransferProgress } from '@/api/types'
import { duration, eta, sizeText } from './format'
import { t } from '@/i18n'

export const restoreActionNames: Record<RestoreAction, string> = { cancel: t('restore.action.cancel'), retry: t('restore.action.retry'), purge_original: t('restore.action.purge') }

export const isLtx = (model: string) => model.startsWith('ltx')

const stageNames: Record<string, string> = {
  downloading: t('restore.stage.downloading'), uploading: t('restore.stage.uploading'), processing: t('restore.stage.processing'), preparing: t('restore.stage.preparing'), queued: t('restore.stage.queued'),
}
export const stageName = (stage: string) => stageNames[stage] || stage

/** “12.3% · 1.2 GB / 9.8 GB · 20 MB/s · 剩余 5分” */
export function transferText(progress: TransferProgress): string {
  const parts = [`${sizeText(progress.transferred_bytes)} / ${sizeText(progress.total_bytes)}`]
  if (progress.percent != null) parts.unshift(`${Number(progress.percent).toFixed(1)}%`)
  if (progress.bytes_per_second) parts.push(`${sizeText(progress.bytes_per_second)}/s`)
  parts.push(t('restore.remaining', { time: eta(progress.eta_seconds) }))
  return parts.join(' · ')
}

export function processingText(progress: TransferProgress): string {
  const fps = progress.fps ? `${Number(progress.fps).toFixed(1)} fps · ` : ''
  return t('restore.processing', { fps, remaining: eta(progress.eta_seconds), elapsed: duration(progress.elapsed_seconds) })
}
