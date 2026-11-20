// 提醒（云实例关机等事件）：未读数、新提醒的弹出提示和提示音。
import { ref, watch } from 'vue'
import { api, type NoticeSummary } from '@/api'
import { toast } from '@/lib/feedback'
import { status } from './status'
import { t } from '@/i18n'

export const unread = ref(0)
/** 已经提示过的最新提醒 ID；null 表示页面刚打开，还没有基准。 */
let lastId: number | null = null
let audio: AudioContext | null = null

export function setNoticeSummary(summary: NoticeSummary): void {
  unread.value = summary.unread
  document.title = summary.unread ? `(${summary.unread}) VideoTurbo` : 'VideoTurbo'
}

function beep(strong: boolean): void {
  try {
    audio ??= new AudioContext()
    const context = audio
    ;(strong ? [880, 660, 880] : [740]).forEach((frequency, index) => {
      const oscillator = context.createOscillator(), gain = context.createGain(), start = context.currentTime + index * 0.18
      oscillator.frequency.value = frequency
      oscillator.connect(gain)
      gain.connect(context.destination)
      gain.gain.setValueAtTime(0.0001, start)
      gain.gain.exponentialRampToValueAtTime(0.2, start + 0.02)
      gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.16)
      oscillator.start(start)
      oscillator.stop(start + 0.17)
    })
  } catch {
    // 浏览器不允许播放时静默跳过。
  }
}

async function check(summary: NoticeSummary): Promise<void> {
  setNoticeSummary(summary)
  const previous = lastId
  lastId = summary.latest_id
  // 第一次只记下基准；ID 变小说明提醒被清理过，同样重新记基准。
  if (previous === null || summary.latest_id <= previous) return
  try {
    const items = (await api.noticesAfter(previous)).items || []
    const last = items[items.length - 1]
    if (!last) return
    toast.info(t('common.titleMessage', { title: last.title, message: last.message }))
    if (status.value?.ui?.notice_sound !== false) beep(items.some((item) => item.level === 'error' || item.level === 'success'))
  } catch {
    // 下一轮状态刷新会再试。
  }
}

export function watchNotices(): void {
  watch(() => status.value?.cloud?.notices, (summary) => {
    if (summary) void check(summary)
  })
  // 浏览器要求先有用户操作才能播放声音。
  document.addEventListener('click', () => {
    try {
      audio ??= new AudioContext()
      void audio.resume()
    } catch {
      // 忽略
    }
  }, { once: true })
}
