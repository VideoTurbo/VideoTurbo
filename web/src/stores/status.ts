// 主控总状态：每 5 秒轮询一次 /api/status，所有页面共用。
import { computed, ref, shallowRef } from 'vue'
import { api, type Status } from '@/api'
import { clockSeconds } from '@/lib/format'
import { toast } from '@/lib/feedback'
import { machineTitle, workerLabel } from '@/lib/workers'
import { t } from '@/i18n'

const POLL_MS = 5000

export const status = shallowRef<Status | null>(null)
/** 每次成功刷新后加一；页面监听它来刷新自己的列表。 */
export const tick = ref(0)
export const connected = ref<boolean | null>(null)
export const refreshedAt = ref('')

export const heartbeatTimeout = computed(() => status.value?.heartbeat_timeout_seconds || 90)
export const showPosters = computed(() => status.value?.ui?.show_posters === true)
export const cloud = computed(() => status.value?.cloud)
/** 桌面版：设置在托盘菜单里改，提示文案不提 docker-compose.yml。 */
export const isDesktop = computed(() => status.value?.deployment === 'desktop')

let pending: Promise<Status | null> | null = null

/** 立即刷新状态。并发调用共用同一次请求。 */
export function refreshStatus(): Promise<Status | null> {
  pending ??= api
    .status()
    .then((data) => {
      status.value = data
      connected.value = true
      refreshedAt.value = clockSeconds(Date.now())
      tick.value++
      return data
    })
    .catch((error: unknown) => {
      // 连续失败时只在刚断开的那一次提示，避免每 5 秒弹一次。
      if (connected.value !== false) toast.error(t('app.statusFailed'), error)
      connected.value = false
      return null
    })
    .finally(() => {
      pending = null
    })
  return pending
}

export function startPolling(): void {
  void refreshStatus()
  setInterval(() => void refreshStatus(), POLL_MS)
}

// ---- 依赖当前 Worker 列表的名称 ----

/** 机器的显示名：显卡型号 + 标识前 4 位。 */
export const titleMachine = (key: string) => machineTitle(key, status.value?.workers || [])
/** Worker 实例的显示名。 */
export const labelWorker = (name: string | null | undefined) => workerLabel(name, status.value?.workers || [])
