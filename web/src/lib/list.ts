// 列表页共用：跟随状态轮询刷新、地址栏参数、防抖。
import { onActivated, onDeactivated, ref, watch, type Ref } from 'vue'
import { tick } from '@/stores/status'

export const PAGE_SIZE = 20

/** 页面显示时立即加载一次，之后每次状态轮询成功都重新加载；切到别的页面后停止。 */
export function useLiveReload(load: () => unknown): void {
  let active = false
  onActivated(() => {
    active = true
    load()
  })
  onDeactivated(() => {
    active = false
  })
  watch(tick, () => {
    if (active) load()
  })
}

/** 输入停止 delay 毫秒后才更新的副本，用于搜索框。 */
export function debounced<T>(source: Ref<T>, delay = 300): Ref<T> {
  const value = ref(source.value) as Ref<T>
  let timer: ReturnType<typeof setTimeout>
  watch(source, (next) => {
    clearTimeout(timer)
    timer = setTimeout(() => {
      value.value = next
    }, delay)
  })
  return value
}

export const queryParam = (key: string) => new URLSearchParams(location.search).get(key)

/** 更新地址栏参数；值为空的参数会被移除。不产生历史记录。 */
export function setQueryParams(params: Record<string, string | undefined>): void {
  const url = new URL(location.href)
  for (const [key, value] of Object.entries(params)) {
    if (value) url.searchParams.set(key, value)
    else url.searchParams.delete(key)
  }
  history.replaceState(null, '', url)
}
