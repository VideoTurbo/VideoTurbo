// Worker 的分组与命名规则。
import type { AsrMode, AsrSettings, Worker } from '@/api/types'
import { secondsSince } from './format'
import { t } from '@/i18n'

/** 同机多实例（名称以 -01、-02 或影片修复的 -dc 结尾）共用一个机器标识。 */
export const machineKey = (name: string) => String(name).replace(/[-_](?:\d{1,3}|dc)$/, '')

/** 实例名里机器标识之后的后缀，如 01、dc。 */
export const instanceSuffix = (name: string) => name.slice(machineKey(name).length).replace(/^[-_]/, '')

const shortGpu = (name: string | null | undefined) => String(name || '').replace(/^NVIDIA\s+(?:GeForce\s+)?/i, '').trim()

/** 机器标识由硬件自动生成（gpu-3f2a9c1d）；界面只显示前 4 位。 */
export function machineTag(key: string): string {
  const match = /^(?:gpu|mac|cpu)-([0-9a-f]{8})$/.exec(key)
  return match ? `#${match[1]!.slice(0, 4)}` : key
}

export function machineTitle(key: string, workers: Worker[]): string {
  const worker = workers.find((item) => machineKey(item.name) === key)
  return `${worker ? `${shortGpu(worker.gpu_name)} ` : ''}${machineTag(key)}`
}

export function workerLabel(name: string | null | undefined, workers: Worker[]): string {
  if (!name) return ''
  const suffix = instanceSuffix(name)
  return machineTitle(machineKey(name), workers) + (suffix ? `·${suffix}` : '')
}

export function workerOnline(worker: Worker, timeout: number, now = Date.now()): boolean {
  const idle = secondsSince(worker.last_seen_at, now)
  return ['online', 'busy'].includes(worker.status) && Number.isFinite(idle) && idle <= timeout
}

/** Worker 支持的识别模式；只做影片修复的 Worker 为空。 */
export const asrModes = (worker: Worker): AsrMode[] => worker.supported_asr_modes

/** Worker 是否支持当前的识别设置。 */
export function supportsAsr(worker: Worker, settings: AsrSettings | undefined): boolean {
  const mode = settings?.mode || 'whisperjav'
  if (!asrModes(worker).includes(mode)) return false
  const needsEnsemble = mode !== 'faster_whisper' && settings?.whisperjav_ensemble
  return !needsEnsemble || worker.supports_whisperjav_ensemble === true
}

export interface MachineMember {
  worker: Worker
  online: boolean
  /** 多实例时显示的标签；单实例为空。 */
  label: string
}

export interface Machine {
  key: string
  members: MachineMember[]
  onlineCount: number
  busyCount: number
  /** 代表这台机器的实例：优先取在线的第一个。 */
  lead: Worker
  /** 参与字幕识别的实例。独立的影片修复实例（-dc）不声明识别模式。 */
  asrMembers: Worker[]
}

const byName = (a: string, b: string) => a.localeCompare(b, 'zh-CN', { numeric: true })

/** 把 Worker 按机器合并，忙碌的排最前，其次在线的。 */
export function groupMachines(workers: Worker[], timeout: number, now = Date.now()): Machine[] {
  const groups = new Map<string, Worker[]>()
  for (const worker of workers) {
    const key = machineKey(worker.name)
    groups.set(key, [...(groups.get(key) || []), worker])
  }
  const machines = [...groups].map(([key, list]): Machine => {
    const sorted = [...list].sort((a, b) => byName(a.name, b.name))
    const members = sorted.map((worker): MachineMember => {
      const suffix = instanceSuffix(worker.name)
      return {
        worker,
        online: workerOnline(worker, timeout, now),
        label: sorted.length > 1 ? (suffix === 'dc' ? t('restore.title') : suffix) : '',
      }
    })
    const online = members.filter((member) => member.online)
    return {
      key,
      members,
      onlineCount: online.length,
      busyCount: online.filter((member) => member.worker.current_job_id).length,
      lead: (online[0] || members[0]!).worker,
      asrMembers: sorted.filter((worker) => asrModes(worker).length),
    }
  })
  return machines.sort((a, b) =>
    Number(b.busyCount > 0) - Number(a.busyCount > 0) || Number(b.onlineCount > 0) - Number(a.onlineCount > 0) || byName(a.key, b.key))
}
