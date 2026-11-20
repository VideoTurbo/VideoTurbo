// 字幕生成与影片修复共用的列表状态、筛选与配色。
import type { ListStatus, ListView } from '@/api/types'
import { t } from '@/i18n'

export type Tone = 'good' | 'work' | 'warn' | 'bad' | ''

export const statusLabels: Record<ListStatus, string> = {
  pending: t('status.pending'), queued: t('status.queued'), running: t('status.running'), paused: t('status.paused'), failed: t('status.failed'), completed: t('status.completed'),
}

export const statusTones: Record<ListStatus, Tone> = {
  pending: '', queued: 'warn', running: 'work', paused: 'warn', failed: 'bad', completed: 'good',
}

/** 左上角筛选，按需要关注的程度排；默认打开“全部”。 */
export const viewNames: { key: ListView; label: string }[] = [
  { key: 'all', label: t('status.view.all') }, { key: 'active', label: t('status.view.active') }, { key: 'failed', label: t('status.failed') },
  { key: 'pending', label: t('status.pending') }, { key: 'completed', label: t('status.completed') },
]
export const DEFAULT_VIEW: ListView = 'all'
export const isView = (value: string | null): value is ListView => viewNames.some((item) => item.key === value)

/** 列表为空时的提示；item 是“字幕任务”或“影片修复任务”。 */
export function emptyText(view: ListView, item: string): string {
  return {
    all: t('status.empty.all'),
    active: t('status.empty.active', { item }),
    failed: t('status.empty.failed', { item }),
    pending: t('status.empty.pending'),
    completed: t('status.empty.completed'),
  }[view]
}
