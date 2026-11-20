// 一台机器的展示状态和操作，卡片和列表视图共用。
import { computed } from 'vue'
import { api, type ShutdownMode } from '@/api'
import { bindingState, NVENC_TIP, shutdownModeLabel } from '@/lib/cloud'
import { toast } from '@/lib/feedback'
import { asrName } from '@/lib/jobs'
import { asrModes, machineTag, supportsAsr, type Machine } from '@/lib/workers'
import { cloud, refreshStatus, status } from '@/stores/status'
import { t } from '@/i18n'

export function useMachine(props: { machine: Machine }) {
  const lead = computed(() => props.machine.lead)
  const live = computed(() => props.machine.onlineCount > 0)
  const multi = computed(() => props.machine.members.length > 1)
  const binding = computed(() => cloud.value?.bindings?.[props.machine.key])

  const device = computed(() => lead.value.asr_device || lead.value.device_type || 'cuda')
  const deviceLabel = computed(() => (device.value === 'cpu' ? 'CPU' : device.value === 'mps' ? 'MPS' : 'CUDA'))
  const tag = computed(() => binding.value?.instance?.title || binding.value?.instance_title || machineTag(props.machine.key))
  const state = computed(() => {
    if (!live.value) return { tone: 'bad', text: t('compute.machine.offline') }
    const text = props.machine.busyCount ? t('compute.machine.working') : t('compute.machine.online')
    return { tone: props.machine.busyCount ? 'work' : 'good', text: multi.value ? `${text} ${props.machine.onlineCount}/${props.machine.members.length}` : text }
  })
  const utilization = computed(() => (live.value && lead.value.gpu_utilization != null ? lead.value.gpu_utilization : null))
  const memoryFree = computed(() => (live.value && lead.value.memory_free_mb != null ? `${lead.value.memory_free_mb} MB` : '-'))

  // ---- 能力说明 ----
  const capabilities = computed(() => {
    const workers = props.machine.members.map((member) => member.worker)
    const asr = props.machine.asrMembers
    const modes = [...new Set(asr.flatMap(asrModes))].map(asrName).join(' / ')
    const restore = [...new Set(workers.flatMap((worker) => worker.restore_models || []))].map((model) => (model === 'ltx' ? 'LTX' : 'BasicVSR++')).join(' / ')
    // 同机实例共用一张卡，编码器检查结果一致，取第一条原因显示。
    const reason = restore ? '' : workers.map((worker) => worker.restore_unavailable_reason).find(Boolean) || ''
    const nvenc = reason.startsWith(t('compute.machine.nvencUnavailable'))
    return {
      asr: modes ? t('compute.machine.asrModes', { modes, dual: asr.some((worker) => worker.supports_whisperjav_ensemble) ? t('compute.machine.dualPass') : '' }) : t('compute.machine.noAsr'),
      restore,
      reason: nvenc ? `${reason}\n\n${NVENC_TIP}` : reason,
      reasonShort: nvenc ? t('compute.machine.nvencAsrOnly') : reason,
      unsupported: asr.some((worker) => !supportsAsr(worker, status.value?.asr)),
    }
  })

  // ---- 版本：同机实例可能是不同版本，全部列出；与主控不一致时提示更新 ----
  const version = computed(() => {
    const versions = [...new Set(props.machine.members.map((member) => member.worker.version))].sort()
    const server = status.value?.version
    const stale = server ? versions.filter((item) => item !== server) : []
    return {
      text: versions.map((item) => `v${item}`).join(' / '),
      mismatch: stale.length ? t('compute.machine.versionMismatch', { versions: stale.map((item) => `v${item}`).join(' / '), server }) : '',
    }
  })

  // ---- 云实例绑定 ----
  const cloudState = computed(() => (binding.value ? bindingState(binding.value) : null))
  const instanceText = computed(() => {
    const b = binding.value
    if (!b) return ''
    if (b.instance) return `${b.instance.title || b.instance.id.slice(0, 8)} · ${b.instance.status_text}`
    return b.instance_title || b.instance_id.slice(0, 8)
  })

  async function setMode(mode: ShutdownMode): Promise<void> {
    try {
      await api.setShutdownMode(props.machine.key, mode)
      toast.ok(t('compute.machine.shutdownMode', { key: props.machine.key, mode: shutdownModeLabel(mode) }))
    } catch (error) {
      toast.error(t('common.setFailed'), error)
    }
    await refreshStatus()
  }

  return { lead, live, multi, binding, device, deviceLabel, tag, state, utilization, memoryFree, capabilities, version, cloudState, instanceText, setMode }
}
