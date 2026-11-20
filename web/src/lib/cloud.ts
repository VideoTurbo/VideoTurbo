// 云实例、自动关机和创建任务的展示规则。创建任务并入对应的实例显示。
import type {
  CloudBinding, CloudInstance, CloudInstanceGroup, GpuConfiguration, GpuOffer, Launch, LaunchState, ShutdownMode,
} from '@/api/types'
import { clock, duration, secondsSince } from './format'
import type { Tone } from './status'
import { t } from '@/i18n'

/** 显卡视频编码（NVENC）检查没通过时的白话说明。 */
export const NVENC_TIP = t('cloud.nvencHelp')

export const shutdownModeOptions: { value: ShutdownMode; label: string }[] = [
  { value: 'always', label: t('cloud.shutdown.always') }, { value: 'off', label: t('cloud.shutdown.off') },
]
export const shutdownModeLabel = (mode: ShutdownMode) => shutdownModeOptions.find((item) => item.value === mode)!.label

/** 机器卡片上的自动关机状态。 */
export function bindingState(binding: CloudBinding, now = Date.now()): { tone: Tone; text: string } {
  const b = binding
  if (b.provider_missing) return { tone: 'bad', text: t('cloud.binding.noPlugin') }
  if (!b.configured) return { tone: 'warn', text: t('cloud.binding.noLogin') }
  if (b.state === 'stopped' || b.instance?.status === 'stopped') return { tone: '', text: t('cloud.binding.stopped') }
  if (b.state === 'stopping') return { tone: 'work', text: t('cloud.binding.stopping') }
  if (b.state === 'failed') {
    const retry = b.next_attempt_at ? t('cloud.binding.retryAt', { time: clock(b.next_attempt_at) }) : ''
    return { tone: 'bad', text: t('cloud.binding.failed', { retry, error: b.last_error || t('cloud.binding.unknownError') }) }
  }
  if (b.state === 'draining') return { tone: 'work', text: t('cloud.binding.draining') }
  if (b.tunnel_error && b.instance?.status === 'running') return { tone: 'warn', text: t('cloud.binding.tunnel', { error: b.tunnel_error }) }
  if (b.last_error) return { tone: 'warn', text: b.last_error }
  if (b.can_shutdown === false) return { tone: '', text: t('cloud.binding.manual') }
  if (b.shutdown_mode === 'off') return { tone: '', text: t('cloud.binding.off') }
  if (b.idle_since) {
    const minutes = Math.max(0, Math.floor((now - new Date(b.idle_since).getTime()) / 60000))
    return { tone: 'warn', text: t('cloud.binding.idle', { minutes, limit: b.idle_minutes }) }
  }
  return { tone: '', text: t('cloud.binding.watching', { limit: b.idle_minutes }) }
}

// ---- 创建实例 ----
export const launchLabels: Record<LaunchState, string> = {
  grabbing: t('cloud.launch.grabbing'), booting: t('cloud.launch.booting'), deploying: t('cloud.launch.deploying'), running: t('cloud.launch.running'), deploy_failed: t('cloud.launch.deployFailed'),
  nvenc_failed: t('cloud.launch.nvencFailed'), failed: t('cloud.launch.failed'), cancelled: t('cloud.launch.cancelled'),
}
export const launchTones: Record<LaunchState, Tone> = {
  grabbing: 'work', booting: 'work', deploying: 'work', running: 'good', deploy_failed: 'bad', nvenc_failed: 'bad', failed: 'bad', cancelled: '',
}
export const launchActive = (state: LaunchState) => state === 'grabbing' || state === 'booting' || state === 'deploying'

export const serviceOptions = [{ value: 'asr', label: t('cloud.service.asr') }, { value: 'restore', label: t('cloud.service.restore') }]
const serviceLabel = (id: string) => serviceOptions.find((item) => item.value === id)?.label || id
export const servicesText = (services: string[] | undefined) =>
  services?.length ? t('cloud.services', { list: services.map(serviceLabel).join(' + ') }) : t('cloud.startOnly')

/** 创建任务实际部署的服务：NVENC 检查未通过后只继续字幕识别时，不再包含影片修复。 */
export const launchServicesText = (launch: Pick<Launch, 'services' | 'asr_only'>) =>
  servicesText(launch.asr_only ? ['asr'] : launch.services)

/** 创建任务当前可以做的后续操作。 */
export function launchActions(launch: Launch) {
  const deployed = !!launch.instance_id
  return {
    /** 影片修复需要的 NVENC 检查未通过：同时选了字幕识别时，可以只继续部署字幕。 */
    continueAsr: deployed && launch.state === 'nvenc_failed' && launch.services.includes('asr') && launch.services.includes('restore'),
    redeploy: deployed && ['deploy_failed', 'nvenc_failed', 'running', 'cancelled'].includes(launch.state) && launch.services.length > 0,
    /** 主机密钥变了导致部署失败时，允许用户确认后信任新密钥。 */
    trustHostKey: deployed && launch.state === 'deploy_failed' && !!launch.host_key_changed,
  }
}

// ---- 实例与创建任务合并展示 ----
/** 云实例区块的一项：一台实例连同它最新的创建任务；还没有实例的任务单独成项。 */
export interface InstanceEntry {
  key: string
  provider: string
  providerLabel: string
  capabilities: string[]
  launchMethods: string[]
  instance?: CloudInstance
  launch?: Launch
}

const launchInstanceId = (launch: Launch) => launch.instance_id || launch.target_instance_id || ''

/**
 * 把创建任务并入对应的实例，每台实例只取最新一条（launches 按创建时间从新到旧）。
 * 还没抢到实例的任务、以及实例已不在列表中的任务单独成项，排在最前。
 * loaded 为 false（实例列表还没读到）时，先不显示已有实例的任务，避免它们闪现成单独一项。
 */
export function instanceEntries(
  groups: CloudInstanceGroup[], launches: Launch[], providerLabel: (provider: string) => string, loaded = true,
): InstanceEntry[] {
  const latest = new Map<string, Launch>()
  const pending: Launch[] = []
  for (const launch of launches) {
    const id = launchInstanceId(launch)
    if (!id) pending.push(launch)
    else if (!latest.has(`${launch.provider}/${id}`)) latest.set(`${launch.provider}/${id}`, launch)
  }
  const entries = groups.flatMap((group) => group.instances.map((instance): InstanceEntry => {
    const key = `${group.provider}/${instance.id}`
    const launch = latest.get(key)
    latest.delete(key)
    return {
      key, provider: group.provider, providerLabel: group.label, instance, launch,
      capabilities: group.capabilities || [], launchMethods: group.launch_methods || [],
    }
  }))
  const orphans = [...pending, ...(loaded ? latest.values() : [])]
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .map((launch): InstanceEntry => ({
      key: `launch/${launch.id}`, provider: launch.provider, providerLabel: providerLabel(launch.provider), launch,
      capabilities: [], launchMethods: [],
    }))
  return [...orphans, ...entries]
}

/** 实例上可以做的操作。 */
export type InstanceAction =
  | 'stop' | 'continueAsr' | 'redeploy' | 'trustHostKey' | 'log' | 'shutdown' | 'start' | 'destroy' | 'forget' | 'clear'

/** 一项实例在卡片和列表两种视图里共用的展示内容。创建任务不单独出现，只体现为实例的状态、部署情况和可做的操作。 */
export interface InstanceCard {
  key: string
  provider: string
  instance?: CloudInstance
  launch?: Launch
  title: string
  /** 标题下的小字：实例 ID；还没有实例时是创建所用的镜像等目标。 */
  sub: string
  /** 实例所处的阶段：抢卡中、开机中、部署中优先于平台状态。 */
  tone: Tone
  statusText: string
  platform: string
  gpu: string
  region: string
  uptime: string
  machine: string
  meta: [string, string][]
  /** 部署情况；实例不是由主控创建或开机的，没有记录时为空。 */
  deploy: { tone: Tone; text: string; detail: string } | null
  hint: string
  /** 当前最该做的一个操作，直接显示成按钮；其余收进“更多”菜单。 */
  primary: InstanceAction | null
  menu: (InstanceAction | '-')[]
}

const serviceNames = (launch: Launch) => (launch.asr_only ? ['asr'] : launch.services).map(serviceLabel).join(' + ')

/** 部署情况：部署了什么、进行到哪一步。 */
function deployInfo(launch: Launch, hasInstance: boolean, now: number): NonNullable<InstanceCard['deploy']> {
  const services = serviceNames(launch)
  const elapsed = t('cloud.launch.elapsed', { time: duration(secondsSince(launch.grabbed_at || launch.created_at, now)) })
  const tried = launch.attempts && launch.attempts > 1 ? t('cloud.launch.attempts', { attempts: launch.attempts }) : ''
  const detail = (...parts: string[]) => [launch.message || '', ...parts].filter(Boolean).join(' · ')
  const plan = services ? t('cloud.launch.willInstall', { services }) : t('cloud.launch.noInstall')
  switch (launch.state) {
    case 'grabbing': return { tone: 'work', text: plan, detail: detail(tried, elapsed) }
    case 'booting': return { tone: 'work', text: plan, detail: detail(elapsed) }
    case 'deploying': return { tone: 'work', text: t('cloud.launch.installing', { services }), detail: detail(elapsed) }
    case 'running': return { tone: 'good', text: services ? t('cloud.launch.installed', { services }) : t('cloud.launch.startedOnly'), detail: detail() }
    case 'deploy_failed': return { tone: 'bad', text: t('cloud.launch.installFailed', { services }), detail: detail() }
    case 'nvenc_failed': return { tone: 'bad', text: t('cloud.launch.nvencStopped'), detail: detail(NVENC_TIP) }
    case 'cancelled': return { tone: 'warn', text: launch.instance_id ? t('cloud.launch.installStopped', { services }) : hasInstance ? t('cloud.launch.bootStopped') : t('cloud.launch.grabStopped'), detail: detail() }
    default: return { tone: 'bad', text: hasInstance ? t('cloud.launch.bootFailed') : t('cloud.launch.createFailed'), detail: detail(tried) }
  }
}

export function instanceCard(entry: InstanceEntry, machineTitle: (key: string) => string, now = Date.now()): InstanceCard {
  const { instance, launch } = entry
  const base = { key: entry.key, provider: entry.provider, launch, platform: entry.providerLabel || '—' }
  const busy = !!launch && launchActive(launch.state)
  const deploy = launch ? deployInfo(launch, !!instance, now) : null
  const stage = busy ? { tone: 'work' as Tone, statusText: launchLabels[launch.state] } : null
  if (!instance) {
    // 还没有实例：正在抢卡，或创建已结束、实例不在平台的列表里。
    const task = launch!
    const gpu = task.gpu_label + (task.gpu_count > 1 ? ` ×${task.gpu_count}` : '')
    const ended = task.instance_id ? { tone: 'warn' as Tone, statusText: t('cloud.instance.gone') }
      : task.state === 'cancelled' ? { tone: '' as Tone, statusText: t('cloud.instance.stopped') } : { tone: 'bad' as Tone, statusText: t('cloud.launch.createFailed') }
    return {
      ...base, ...(stage || ended), title: gpu, sub: task.target_label, deploy, hint: '',
      gpu, region: '—', uptime: '—', machine: task.machine ? machineTitle(task.machine) : '—',
      meta: [[t('compute.instances.col.platform'), base.platform], [t('compute.instances.col.method'), task.target_label]],
      primary: busy ? 'stop' : 'clear', menu: ['log'],
    }
  }
  const can = (capability: string) => entry.capabilities.includes(capability)
  const running = instance.status === 'running', stopped = instance.status === 'stopped'
  const gpu = (instance.gpu_name || '—') + (instance.gpu_count > 1 ? ` ×${instance.gpu_count}` : '')
  const uptime = running && instance.started_at ? duration(now / 1000 - instance.started_at) : '—'
  const machine = instance.machine ? machineTitle(instance.machine) : t('cloud.unbound')
  // 不支持关机接口的平台（手动 SSH）只能在平台网页上关机。
  const manual = !can('shutdown')
  let hint = ''
  if (running && !instance.machine && !busy) hint = manual ? t('cloud.hint.createToInstall') : t('cloud.hint.bindToShutdown')
  else if (manual && instance.machine) hint = t('cloud.hint.manualShutdown')

  const deployActions = launch ? launchActions(launch) : null
  const available: Record<InstanceAction, boolean> = {
    stop: busy,
    // 部署要通过 SSH 登录实例，只有运行中才能做；已关机时先开机。
    continueAsr: running && !!deployActions?.continueAsr,
    redeploy: running && !!deployActions?.redeploy,
    trustHostKey: running && !!deployActions?.trustHostKey,
    log: !!launch,
    shutdown: can('shutdown') && (running || instance.status === 'starting'),
    // 已有任务在给这台实例开机时，不再重复发起，也不能销毁。
    start: stopped && !busy && can('launch') && entry.launchMethods.includes('startup'),
    destroy: stopped && !busy && can('destroy'),
    forget: can('forget'),
    clear: !!launch && !busy,
  }
  // 最该做的一件事：进行中先给“停止”，部署出问题先给补救办法，其余按开关机。
  const failed = launch?.state === 'deploy_failed' || launch?.state === 'nvenc_failed'
  const order: InstanceAction[] = ['stop', 'continueAsr', ...(failed ? ['redeploy' as const] : []), 'shutdown', 'start']
  const primary = order.find((action) => available[action]) || null
  const groups: InstanceAction[][] = [['log', 'continueAsr', 'redeploy', 'trustHostKey', 'clear'], ['shutdown', 'start'], ['destroy', 'forget']]
  const menu = groups.map((group) => group.filter((action) => available[action] && action !== primary))
    .filter((group) => group.length)
    .flatMap((group, index): (InstanceAction | '-')[] => (index ? ['-', ...group] : group))
  return {
    ...base, instance, hint, gpu, uptime, machine, deploy, primary, menu,
    title: instance.title || t('cloud.instance.unnamed'), sub: instance.id,
    ...(stage || { tone: running ? 'good' : stopped ? '' : 'work', statusText: instance.status_text }),
    region: instance.region || '—',
    meta: [
      [t('compute.instances.col.platform'), base.platform], ['GPU', gpu],
      ...(instance.region ? [[t('compute.instances.col.region'), instance.region] as [string, string]] : []),
      [t('compute.instances.col.uptime'), uptime], [t('compute.instances.col.machine'), machine],
    ],
  }
}

/** 操作的按钮文字，按实例当前所处的阶段措辞。 */
export function instanceActionLabel(card: InstanceCard, action: InstanceAction): string {
  const state = card.launch?.state
  switch (action) {
    case 'stop': return state === 'deploying' ? t('cloud.action.stopInstall') : state === 'booting' ? t('cloud.action.stop') : card.instance ? t('cloud.action.stopBoot') : t('cloud.action.stopGrab')
    case 'continueAsr': return t('cloud.action.continueAsr')
    case 'redeploy': return t('cloud.action.redeploy')
    case 'trustHostKey': return t('cloud.action.trust')
    case 'log': return t('compute.instances.viewLog')
    case 'shutdown': return t('cloud.action.shutdown')
    case 'start': return t('cloud.action.start')
    case 'destroy': return t('cloud.action.destroy')
    case 'forget': return t('cloud.action.forget')
    case 'clear': return card.instance ? t('cloud.action.clearLaunch') : t('cloud.action.remove')
  }
}

/** 型号在所选卡数下的配置；没有对应卡数时取第一档。 */
export function gpuConfiguration(offer: GpuOffer | undefined, count: number | null): GpuConfiguration | undefined {
  const configs = offer?.configurations || []
  return configs.find((config) => config.gpu_count === count) || configs[0]
}

const yuan = (price: number) => t('cloud.price.perHour', { price: Number(price).toFixed(2) })

export function gpuLabel(offer: GpuOffer, count: number | null): string {
  const config = gpuConfiguration(offer, count)
  const price = config ? config.price_per_hour : offer.price_per_hour
  const available = config ? config.available : offer.available
  const priceText = price != null ? ` · ${yuan(price)}${config ? `（${config.gpu_count} 卡合计）` : ''}` : t('cloud.price.unavailableSuffix')
  return offer.label + priceText + (available === false ? t('cloud.price.outOfStockSuffix') : '')
}

export function gpuPriceHint(offer: GpuOffer | undefined, count: number | null): string {
  if (!offer) return ''
  const config = gpuConfiguration(offer, count)
  const price = config ? config.price_per_hour : offer.price_per_hour
  const parts = [price != null ? (config ? t('cloud.price.total', { count: config.gpu_count, price: yuan(price) }) : t('cloud.price.reference', { price: yuan(price) })) : t('cloud.price.unavailable')]
  if (config?.detail) parts.push(config.detail)
  if ((config ? config.available : offer.available) === false) parts.push(t('cloud.price.outOfStock'))
  parts.push(t('cloud.price.note'))
  return t('cloud.price.sentence', { text: parts.join(t('common.listJoin')) })
}

/** 型号可选的卡数：有分档配置时取分档，否则用平台通用的卡数列表。 */
export const gpuCountChoices = (offer: GpuOffer | undefined, fallback: number[]) =>
  offer?.configurations?.length ? offer.configurations.map((config) => config.gpu_count) : fallback
