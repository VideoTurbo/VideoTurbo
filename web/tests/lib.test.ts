import { describe, expect, it } from 'vitest'
import type { CloudBinding, CloudInstanceGroup, GpuOffer, Job, Launch, Worker } from '@/api/types'
import {
  bindingState, gpuCountChoices, gpuLabel, gpuPriceHint, instanceActionLabel, instanceCard, instanceEntries, launchActions, launchActive, launchLabels,
  launchServicesText, servicesText,
} from '@/lib/cloud'
import { duration, shortDate, sizeText } from '@/lib/format'
import { compactModelMix, modelMix, nextSort, primaryJobAction } from '@/lib/jobs'
import { transferText } from '@/lib/restore'
import { emptyText, isView, viewNames } from '@/lib/status'
import { toConfigPayload, toFormValues } from '@/lib/settings'
import { groupMachines, machineKey, machineTag, supportsAsr, workerLabel } from '@/lib/workers'

const now = Date.parse('2026-01-01T00:00:00Z')
const seen = (secondsAgo: number) => new Date(now - secondsAgo * 1000).toISOString()
const worker = (name: string, extra: Partial<Worker> = {}): Worker =>
  ({ name, version: '0.1.0', status: 'online', last_seen_at: seen(5), gpu_name: 'NVIDIA GeForce RTX 5090', supported_asr_modes: ['whisperjav'], ...extra })

describe('format', () => {
  it('formats durations and sizes', () => {
    expect(duration(59)).toBe('59秒')
    expect(duration(600)).toBe('10分0秒')
    expect(duration(3725)).toBe('1时2分')
    expect(sizeText(512)).toBe('512 B')
    expect(sizeText(1536)).toBe('1.5 KB')
    expect(sizeText(5 * 1024 ** 3)).toBe('5.00 GB')
    expect(shortDate(null)).toBe('—')
  })
})

describe('jobs', () => {
  it('toggles sort direction on the same column and uses per-column defaults', () => {
    expect(nextSort('updated_desc', 'updated')).toBe('updated_asc')
    expect(nextSort('updated_desc', 'name')).toBe('name_asc')
    expect(nextSort('name_asc', 'size')).toBe('size_desc')
  })

  it('picks the most useful row action from what the server allows', () => {
    const action = (...actions: Job['actions']) => primaryJobAction({ actions })
    expect([action('start', 'skip'), action('resume', 'cancel'), action('retry', 'priority'), action('pause', 'cancel'), action('download', 'reprocess')])
      .toEqual(['start', 'resume', 'retry', 'pause', 'download'])
    // 扫描判定为失败的任务后端不给重试，也就没有主操作。
    expect(action('reprocess')).toBeNull()
  })

  it('lists filters with "all" first and validates view names', () => {
    expect(viewNames.map((item) => item.label)).toEqual(['全部', '进行中', '失败', '待处理', '已完成'])
    expect([isView('completed'), isView('done'), isView(null)]).toEqual([true, false, false])
    expect(emptyText('failed', '字幕任务')).toBe('没有失败的字幕任务')
  })

  it('summarises which models produced the translation', () => {
    const stats = { provider_cue_counts: { grok: 82, deepseek: 18, gemini: 0 }, provider_percentages: { grok: 82, deepseek: 18 } }
    expect(modelMix(stats)).toBe('模型占比：Grok 82.0%（82 条） · DeepSeek 18.0%（18 条）')
    expect(compactModelMix(stats)).toBe('Grok 82% · DeepSeek 18%')
    expect(modelMix(null)).toBe('')
  })
})

describe('workers', () => {
  it('derives machine identity from instance names', () => {
    expect(machineKey('gpu-3f2a9c1d-01')).toBe('gpu-3f2a9c1d')
    expect(machineKey('gpu-3f2a9c1d-dc')).toBe('gpu-3f2a9c1d')
    expect(machineTag('gpu-3f2a9c1d')).toBe('#3f2a')
    expect(machineTag('custom-box')).toBe('custom-box')
    expect(workerLabel('gpu-3f2a9c1d-02', [worker('gpu-3f2a9c1d-01')])).toBe('RTX 5090 #3f2a·02')
  })

  it('merges instances of one machine and sorts busy machines first', () => {
    const machines = groupMachines([
      worker('gpu-aaaaaaaa-01'),
      worker('gpu-bbbbbbbb-02', { current_job_id: 'job' }),
      worker('gpu-bbbbbbbb-01', { last_seen_at: seen(500) }),
      worker('gpu-bbbbbbbb-dc', { supported_asr_modes: [] }),
      worker('gpu-cccccccc-01', { status: 'offline' }),
    ], 90, now)
    expect(machines.map((machine) => machine.key)).toEqual(['gpu-bbbbbbbb', 'gpu-aaaaaaaa', 'gpu-cccccccc'])
    const busy = machines[0]!
    expect(busy.members.map((member) => [member.label, member.online])).toEqual([['01', false], ['02', true], ['影片修复', true]])
    expect([busy.onlineCount, busy.busyCount, busy.lead.name]).toEqual([2, 1, 'gpu-bbbbbbbb-02'])
    // 独立的影片修复实例不参与识别能力展示。
    expect(busy.asrMembers.map((item) => item.name)).toEqual(['gpu-bbbbbbbb-01', 'gpu-bbbbbbbb-02'])
    expect(machines[1]!.members[0]!.label).toBe('')
  })

  it('checks whether a worker supports the current recognition settings', () => {
    const modern = worker('a', { supported_asr_modes: ['whisperjav'], supports_whisperjav_ensemble: true })
    expect(supportsAsr(modern, { mode: 'whisperjav', whisperjav_ensemble: true })).toBe(true)
    expect(supportsAsr(worker('b', { supported_asr_modes: [] }), { mode: 'whisperjav', whisperjav_ensemble: false })).toBe(false)
    expect(supportsAsr(worker('c', { supported_asr_modes: ['whisperjav'] }), { mode: 'whisperjav', whisperjav_ensemble: true })).toBe(false)
  })
})

describe('restore', () => {
  it('describes transfer progress', () => {
    expect(transferText({ stage: 'downloading', percent: 12.34, transferred_bytes: 1024 ** 3, total_bytes: 8 * 1024 ** 3, bytes_per_second: 20 * 1024 ** 2, eta_seconds: 300 }))
      .toBe('12.3% · 1.00 GB / 8.00 GB · 20.0 MB/s · 剩余 5分0秒')
  })
})

describe('cloud', () => {
  const tiered: GpuOffer = {
    id: '5090', label: 'RTX 5090',
    configurations: [{ gpu_count: 1, price_per_hour: 2.5 }, { gpu_count: 2, price_per_hour: 5, available: false, detail: '32 核' }],
  }
  const flat: GpuOffer = { id: '4090', label: 'RTX 4090', price_per_hour: null }

  it('labels GPU offers with price, card count and stock', () => {
    expect(gpuLabel(tiered, 1)).toBe('RTX 5090 · ¥2.50/时（1 卡合计）')
    expect(gpuLabel(tiered, 2)).toBe('RTX 5090 · ¥5.00/时（2 卡合计） · 平台标记无货')
    expect(gpuLabel(flat, 1)).toBe('RTX 4090 · 价格暂不可用')
    expect(gpuPriceHint(tiered, 2)).toBe('2 卡合计 ¥5.00/时；32 核；平台标记无货，可按设置的间隔重试；参考价不含额外存储，以平台实际结算为准。')
    expect(gpuPriceHint(undefined, 1)).toBe('')
  })

  it('offers only the card counts a model supports', () => {
    expect(gpuCountChoices(tiered, [1, 2, 4])).toEqual([1, 2])
    expect(gpuCountChoices(flat, [1, 2, 4])).toEqual([1, 2, 4])
    expect(servicesText(['asr', 'restore'])).toBe('服务：字幕识别 + 影片修复')
    expect(servicesText([])).toBe('只开机')
  })

  it('offers subtitle-only continuation after a failed NVENC check only when subtitles were selected', () => {
    const launch = (extra: Partial<Launch>): Launch => ({
      id: 'test', provider: 'p', state: 'nvenc_failed', instance_id: 'i-1', services: ['asr', 'restore'], gpu_label: '5090', gpu_count: 1,
      target_label: 't', created_at: new Date(now).toISOString(), ...extra,
    })
    expect(launchLabels.nvenc_failed).toBe('显卡视频编码检查未通过 · 已停止安装')
    expect(launchActive('nvenc_failed')).toBe(false)
    expect(launchActions(launch({}))).toEqual({ continueAsr: true, redeploy: true, trustHostKey: false })
    expect(launchActions(launch({ services: ['restore'] })).continueAsr).toBe(false)
    expect(launchActions(launch({ state: 'deploy_failed' })).continueAsr).toBe(false)
    expect(launchActions(launch({ instance_id: null })).continueAsr).toBe(false)
    expect(launchActions(launch({ state: 'deploy_failed', host_key_changed: true })).trustHostKey).toBe(true)
    // 只继续字幕识别后，显示的部署内容不再包含影片修复。
    expect(launchServicesText(launch({ state: 'running', asr_only: true }))).toBe('服务：字幕识别')
    expect(launchServicesText(launch({ state: 'running' }))).toBe('服务：字幕识别 + 影片修复')
  })

  it('merges launches into their instance and keeps launches without an instance as their own entry', () => {
    const at = (minutesAgo: number) => new Date(now - minutesAgo * 60000).toISOString()
    const launch = (id: string, extra: Partial<Launch>): Launch => ({
      id, provider: 'p', state: 'running', services: ['asr'], gpu_label: 'RTX 5090', gpu_count: 2, target_label: '私有镜像',
      created_at: at(1), ...extra,
    })
    const groups: CloudInstanceGroup[] = [{
      provider: 'p', label: '测试云', capabilities: ['shutdown', 'launch', 'destroy'], launch_methods: ['startup'],
      instances: [
        { id: 'i-1', title: '一号', status: 'running', status_text: '运行中', gpu_name: 'RTX 5090', gpu_count: 1, started_at: now / 1000 - 3600 },
        { id: 'i-2', status: 'stopped', status_text: '已关机', gpu_name: 'RTX 4090', gpu_count: 1 },
        { id: 'i-3', status: 'stopped', status_text: '已关机', gpu_count: 1 },
      ],
    }]
    // 从新到旧：抢卡中的新建任务、i-1 的最新任务、i-1 的旧任务、给 i-2 开机的任务、实例已不在列表的任务。
    const launches = [
      launch('grab', { state: 'grabbing', attempts: 3, message: '第 3 次：缺货，继续重试', created_at: at(2) }),
      launch('new', { instance_id: 'i-1', state: 'deploy_failed', created_at: at(3) }),
      launch('old', { instance_id: 'i-1', created_at: at(9) }),
      launch('start', { target_instance_id: 'i-2', state: 'grabbing', created_at: at(4) }),
      launch('gone', { instance_id: 'i-9', state: 'cancelled', created_at: at(5) }),
    ]
    const label = () => '测试云'
    const entries = instanceEntries(groups, launches, label)
    expect(entries.map((entry) => [entry.key, entry.launch?.id])).toEqual([
      ['launch/grab', 'grab'], ['launch/gone', 'gone'], ['p/i-1', 'new'], ['p/i-2', 'start'], ['p/i-3', undefined],
    ])
    // 实例列表还没读到时，不把已有实例的任务当成单独一项。
    expect(instanceEntries([], launches, label, false).map((entry) => entry.key)).toEqual(['launch/grab'])

    const [grab, gone, one, two, three] = entries.map((entry) => instanceCard(entry, (key) => `机器 ${key}`, now))
    // 还在抢卡：状态是抢卡中，只给“停止抢卡”，日志在菜单里。
    expect(grab).toMatchObject({
      title: 'RTX 5090 ×2', sub: '私有镜像', tone: 'work', statusText: '租机中', platform: '测试云', primary: 'stop', menu: ['log'],
      deploy: { tone: 'work', text: '开机后安装：字幕识别', detail: `第 3 次：缺货，继续重试 · 已尝试 3 次 · 已用 ${duration(120)}` },
    })
    expect(instanceActionLabel(grab!, 'stop')).toBe('停止租机')
    expect(gone).toMatchObject({ statusText: '云主机已不在平台上', primary: 'clear', menu: ['log'] })
    expect(instanceActionLabel(gone!, 'clear')).toBe('移除')
    // 运行中但部署失败：状态仍是平台状态，主按钮给补救办法，关机收进菜单。
    expect(one).toMatchObject({
      title: '一号', sub: 'i-1', tone: 'good', statusText: '运行中', uptime: duration(3600), machine: '未绑定',
      deploy: { tone: 'bad', text: '安装失败：字幕识别' }, primary: 'redeploy', menu: ['log', 'clear', '-', 'shutdown'],
    })
    expect(one!.hint).toBe('在“已接入的机器”中绑定后可自动关机')
    expect(instanceActionLabel(one!, 'clear')).toBe('清除安装记录')
    // 正在给已关机的实例开机：显示抢卡中，只能停止，不能再开机或销毁。
    expect(two).toMatchObject({ tone: 'work', statusText: '租机中', primary: 'stop', menu: ['log'] })
    expect(instanceActionLabel(two!, 'stop')).toBe('停止开机')
    expect(three).toMatchObject({ title: '未命名云主机', statusText: '已关机', deploy: null, primary: 'start', menu: ['destroy'] })
    // 部署失败后实例已关机：部署要登录实例，不再给“重新部署”，主按钮是开机。
    const off = instanceCard({ ...entries[4]!, launch: launch('bad', { instance_id: 'i-3', state: 'deploy_failed' }) }, String, now)
    expect(off).toMatchObject({ statusText: '已关机', primary: 'start', menu: ['log', 'clear', '-', 'destroy'] })
    // 部署正常的运行中实例：主按钮是关机。
    const fine = instanceCard({ ...entries[2]!, launch: launch('ok', { instance_id: 'i-1' }) }, String, now)
    expect(fine).toMatchObject({ deploy: { tone: 'good', text: '已安装：字幕识别' }, primary: 'shutdown', menu: ['log', 'redeploy', 'clear'] })
  })

  it('explains the auto-shutdown state of a bound machine', () => {
    const binding = (extra: Partial<CloudBinding>): CloudBinding =>
      ({ provider: 'p', provider_label: 'P', instance_id: 'i', shutdown_mode: 'always', configured: true, idle_minutes: 30, ...extra })
    expect(bindingState(binding({}), now)).toEqual({ tone: '', text: '空闲满 30 分钟后关机' })
    expect(bindingState(binding({ idle_since: seen(600) }), now)).toEqual({ tone: 'warn', text: '已空闲 10 分钟 · 满 30 分钟关机' })
    expect(bindingState(binding({ shutdown_mode: 'off' }), now).text).toBe('不自动关机')
    expect(bindingState(binding({ configured: false }), now).tone).toBe('warn')
    expect(bindingState(binding({ state: 'failed', last_error: '超时' }), now)).toEqual({ tone: 'bad', text: '关机失败：超时' })
  })
})

describe('settings', () => {
  it('converts between saved config and form values', () => {
    const form = toFormValues('scan', { enabled: true, schedule: '5 3 * * *', settle_minutes: 10 })
    expect(form).toEqual({ enabled: true, schedule: '03:05', settle_minutes: 10 })
    expect(toConfigPayload('scan', { ...form, schedule: '21:30' })).toEqual({ enabled: true, schedule: '30 21 * * *', settle_minutes: 10 })
    // 不是每日定时的表达式按默认时间显示。
    expect(toFormValues('scan', { schedule: '*/5 * * * *' }).schedule).toBe('03:00')
  })

  it('validates integers and reports the field name', () => {
    expect(() => toConfigPayload('scan', { enabled: true, schedule: '03:00', settle_minutes: null })).toThrow('新文件等待时间（分钟）不能为空')
    expect(() => toConfigPayload('translation', { enabled: true, job_concurrency: 9 })).toThrow('同时翻译影片数须在 1～4 之间')
  })
})
