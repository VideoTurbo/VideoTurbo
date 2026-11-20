import { t } from '@/i18n'
// 纯展示用的格式化函数。

const pad = (n: number) => String(n).padStart(2, '0')

function parse(value: string | number | null | undefined): Date | null {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

/** 完整时间：2026/1/2 03:04:05 */
export function fullDate(value: string | null | undefined): string {
  return parse(value)?.toLocaleString('zh-CN', { hour12: false }) ?? '—'
}

/** 表格里的短时间：今天只显示时:分，其余显示 月-日 时:分。 */
export function shortDate(value: string | null | undefined): string {
  const date = parse(value)
  if (!date) return '—'
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  return date.toDateString() === new Date().toDateString()
    ? time
    : `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${time}`
}

/** 距今多久：刚刚、5 分钟前、3 小时前、昨天、4 天前，更早显示日期。 */
export function relativeTime(value: string | null | undefined, now = Date.now()): string {
  const date = parse(value)
  if (!date) return '—'
  const minutes = Math.floor((now - date.getTime()) / 60000)
  if (minutes < 1) return t('time.justNow')
  if (minutes < 60) return t('time.minutesAgo', { minutes })
  if (minutes < 24 * 60) return t('time.hoursAgo', { hours: Math.floor(minutes / 60) })
  const days = Math.floor(minutes / (24 * 60))
  if (days === 1) return t('time.yesterday')
  if (days < 30) return t('time.daysAgo', { days })
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

/** 时:分 */
export function clock(value: string | number | null | undefined): string {
  return parse(value)?.toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' }) ?? '—'
}

/** 时:分:秒 */
export function clockSeconds(value: number | Date): string {
  return new Date(value).toLocaleTimeString('zh-CN', { hour12: false })
}

export function duration(value: number | null | undefined): string {
  const seconds = Math.max(0, Math.round(Number(value) || 0))
  if (seconds < 60) return t('time.seconds', { seconds })
  const minutes = Math.floor(seconds / 60)
  return minutes < 60 ? t('time.minutesSeconds', { minutes, seconds: seconds % 60 }) : t('time.hoursMinutes', { hours: Math.floor(minutes / 60), minutes: minutes % 60 })
}

export const eta = (seconds: number | null | undefined) => (seconds == null ? t('time.calculating') : duration(seconds))

export function sizeText(value: number | null | undefined): string {
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = Number(value) || 0
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit++
  }
  return `${unit ? size.toFixed(unit >= 3 ? 2 : 1) : size} ${units[unit]}`
}

export function fileName(path: string | null | undefined): string {
  return String(path || '').split(/[\\/]/).pop() || t('common.unknownVideo')
}

export function hostOf(url: string | null | undefined): string {
  try {
    return new URL(url || '').host
  } catch {
    return url || ''
  }
}

export const clampPercent = (value: unknown) => Math.max(0, Math.min(100, Number(value) || 0))

/** 上次心跳距今的秒数。 */
export const secondsSince = (value: string, now = Date.now()) => (now - new Date(value).getTime()) / 1000

/** 把文本按搜索词拆成片段，hit 为 true 的片段需要高亮。 */
export function highlightParts(text: string, queryText: string): { text: string; hit: boolean }[] {
  const terms = queryText.trim().toLowerCase().split(/\s+/).filter(Boolean)
  if (!terms.length) return [{ text, hit: false }]
  const lower = text.toLowerCase()
  const hit = new Array<boolean>(text.length).fill(false)
  for (const term of terms) {
    for (let index = lower.indexOf(term); index >= 0; index = lower.indexOf(term, index + term.length)) {
      hit.fill(true, index, index + term.length)
    }
  }
  const parts: { text: string; hit: boolean }[] = []
  for (let i = 0; i < text.length; i++) {
    const last = parts[parts.length - 1]
    if (last && last.hit === hit[i]) last.text += text[i]
    else parts.push({ text: text[i]!, hit: hit[i]! })
  }
  return parts
}
