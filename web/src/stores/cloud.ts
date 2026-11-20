// 云实例列表。查询云平台较慢，所以独立于 5 秒一次的状态轮询，按需节流刷新。
import { ref, shallowRef, watch } from 'vue'
import { api, errorMessage, type CloudInstance, type CloudInstanceGroup } from '@/api'
import { launchActive } from '@/lib/cloud'
import { cloud, tick } from './status'
import { t } from '@/i18n'

export const instanceGroups = shallowRef<CloudInstanceGroup[]>([])
/** 最近一次读取实例的时间戳；0 表示还没读过或已作废。 */
export const instancesAt = ref(0)

let loading: Promise<CloudInstanceGroup[]> | null = null

/** 立即读取实例列表；失败时抛出。并发调用共用同一次请求。 */
export function fetchInstances(): Promise<CloudInstanceGroup[]> {
  loading ??= api.cloudInstances()
    .then((groups) => {
      instanceGroups.value = groups
      return groups
    })
    .finally(() => {
      instancesAt.value = Date.now()
      loading = null
    })
  return loading
}

/** 刷新实例列表，失败时把错误显示在列表里。 */
export async function refreshInstances(): Promise<void> {
  try {
    await fetchInstances()
  } catch (error) {
    instanceGroups.value = [{ provider: '', label: t('cloud.platform'), error: errorMessage(error), instances: [] }]
  }
}

export function findInstance(provider: string, id: string): CloudInstance | undefined {
  return instanceGroups.value.find((group) => group.provider === provider)?.instances.find((instance) => instance.id === id)
}

/** 开始跟随状态轮询刷新：平时 30 秒一次，有创建任务在进行时 10 秒一次；创建任务状态变化时立即刷新。 */
export function watchInstances(): void {
  let signature: string | null = null
  watch(tick, () => {
    if (!cloud.value?.providers_configured) return
    const launches = cloud.value.launches || []
    const next = launches.map((launch) => `${launch.id}:${launch.state}`).join(',')
    const changed = signature !== null && next !== signature
    signature = next
    const interval = launches.some((launch) => launchActive(launch.state)) ? 10_000 : 30_000
    if (changed || Date.now() - instancesAt.value >= interval) void refreshInstances()
  })
}
