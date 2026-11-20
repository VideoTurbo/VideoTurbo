// 列表 / 卡片视图的选择，按区块记在浏览器本地。
import { ref, watch, type Ref } from 'vue'

export type ViewMode = 'card' | 'list'

export function useViewMode(name: string): Ref<ViewMode> {
  const key = `video-turbo-view-${name}`
  let stored: string | null = null
  try {
    stored = localStorage.getItem(key)
  } catch {
    // 存储不可用时用默认的卡片视图。
  }
  const mode = ref<ViewMode>(stored === 'list' ? 'list' : 'card')
  watch(mode, (value) => {
    try {
      localStorage.setItem(key, value)
    } catch {
      // 存储不可用时仍可切换，只是刷新后不保留。
    }
  })
  return mode
}
