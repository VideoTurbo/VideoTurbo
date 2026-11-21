// 云主机页各弹窗的开关。卡片、列表和弹窗之间互相打开，所以放在共享模块里。
import { ref } from 'vue'
import type { CloudInstance } from '@/api/types'

/** 正在绑定云实例的机器标识。 */
export const bindingMachine = ref<string | null>(null)
/** 正在查看日志的创建任务 ID。 */
export const launchLogId = ref<string | null>(null)
export const providersOpen = ref(false)
/** 添加显卡电脑（生成接入串）。 */
export const addWorkerOpen = ref(false)

/** 创建实例弹窗；preset 表示给一台已关机的实例开机。 */
export interface LaunchPreset { provider: string; instance: CloudInstance }
export const launchOpen = ref(false)
export const launchPreset = ref<LaunchPreset | null>(null)

export function openLaunch(preset: LaunchPreset | null = null): void {
  launchPreset.value = preset
  launchOpen.value = true
}
