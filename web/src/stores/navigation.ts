// 主导航：当前页面与设置分类。其他页面需要跳到某个设置分类时调用 openSettings。
import { ref } from 'vue'
import { queryParam, setQueryParams } from '@/lib/list'

export type Tab = 'subtitle' | 'restore' | 'compute' | 'translate' | 'settings'
export type SettingsCategory =
  | 'media' | 'media_servers' | 'subtitle' | 'restore' | 'cloud'
  | 'plugins' | 'ui' | 'notifiers' | 'system' | 'account'

const TABS: Tab[] = ['subtitle', 'restore', 'compute', 'translate', 'settings']
const requested = queryParam('tab')
export const tab = ref<Tab>(requested && (TABS as string[]).includes(requested) ? (requested as Tab) : 'subtitle')
/** 设置页下一次激活时要切到的分类；设置页处理后清空。 */
export const settingsRequest = ref<SettingsCategory | null>(null)

export function switchTab(next: Tab): void {
  tab.value = next
  setQueryParams({ tab: next === 'subtitle' ? undefined : next })
}

export function openSettings(category: SettingsCategory): void {
  settingsRequest.value = category
  switchTab('settings')
}

/** 打开提醒指向的位置：页面名（subtitle、translate…），或 settings:设置分类。 */
export function openTarget(target: string): void {
  const [page, category] = target.split(':')
  if (page === 'settings' && category) openSettings(category as SettingsCategory)
  else if ((TABS as string[]).includes(page)) switchTab(page as Tab)
}
