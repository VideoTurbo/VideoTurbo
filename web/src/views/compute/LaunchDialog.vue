<script setup lang="ts">
// 创建实例：选平台、开机方式、目标、GPU 和开机后要部署的服务。
// 选项之间有依赖（平台 → 开机方式 → 目标 → GPU），每一级都在用户改动时由对应的处理函数显式重算。
import { NButton, NInput, NInputNumber, NModal, NSelect } from 'naive-ui'
import { computed, ref, watch } from 'vue'
import { api, type CloudInstance, type LaunchOptions, type LaunchTarget, type ShutdownMode } from '@/api'
import HelpTip from '@/components/HelpTip.vue'
import { gpuConfiguration, gpuCountChoices, gpuLabel, gpuPriceHint, serviceOptions, shutdownModeOptions } from '@/lib/cloud'
import { toast } from '@/lib/feedback'
import { findInstance } from '@/stores/cloud'
import { cloud, refreshStatus } from '@/stores/status'
import { launchOpen, launchPreset, providersOpen, type LaunchPreset } from './dialogs'
import { t } from '@/i18n'

const provider = ref('')
const method = ref('')
const search = ref('')
const target = ref('')
const gpu = ref('')
const count = ref<number | null>(null)
const interval = ref<number | null>(2)
const services = ref<string[]>(['asr'])
const mode = ref<ShutdownMode>('always')

const options = ref<LaunchOptions | null>(null)
const targets = ref<LaunchTarget[]>([])
const targetsState = ref<'idle' | 'loading' | 'failed'>('idle')
const submitting = ref(false)
// 弹窗关闭或选项变化后，丢弃还在路上的旧请求。
let optionsRequest = 0
let targetsRequest = 0

const providers = computed(() => (cloud.value?.launch_providers || []).map((item) => ({ value: item.id, label: item.label })))
const currentMethod = computed(() => options.value?.methods.find((item) => item.id === method.value))
/** 开机方式沿用实例原有配置时，不能选 GPU。 */
const fixedGpu = computed(() => currentMethod.value?.configurable_gpu === false)
const offer = computed(() => options.value?.gpus.find((item) => item.id === gpu.value))
const canShutdown = computed(() => (options.value?.capabilities || []).includes('shutdown'))

const methodOptions = computed(() => (options.value?.methods || []).map((item) => ({ value: item.id, label: item.label })))
const targetOptions = computed(() => targets.value.map((item) => ({ value: item.id, label: item.label + (item.detail ? ` · ${item.detail}` : '') })))
const gpuOptions = computed(() => (options.value?.gpus || []).map((item) => ({ value: item.id, label: gpuLabel(item, count.value) })))
const countOptions = computed(() => gpuCountChoices(offer.value, options.value?.gpu_counts || []).map((n) => ({ value: n, label: String(n) })))

const targetPlaceholder = computed(() => {
  if (targetsState.value === 'loading') return t('common.loading')
  if (targetsState.value === 'failed') return t('common.loadFailed')
  if (currentMethod.value?.searchable && !targets.value.length) return t('compute.launch.searchFirst')
  return targets.value.length ? t('plugins.fields.select') : t('compute.launch.noOptions')
})
const priceHint = computed(() => (fixedGpu.value ? t('compute.launch.keepConfig') : gpuPriceHint(offer.value, count.value)))

/** 所选型号不支持当前卡数时，改成它支持的第一档。 */
function fitCount(): void {
  const choices = gpuCountChoices(offer.value, options.value?.gpu_counts || [])
  if (count.value == null || !choices.includes(count.value)) count.value = choices[0] ?? null
}

/** 给已有实例开机时，GPU 型号和数量跟随实例。 */
function syncStartupGpu(instance?: CloudInstance): void {
  if (!target.value) return
  const known = instance || findInstance(provider.value, target.value)
  if (known) {
    gpu.value = known.gpu_id || ''
    count.value = known.gpu_count > 0 ? known.gpu_count : null
  } else {
    // 实例列表里找不到时，按目标名称里出现的型号名匹配。
    const label = targets.value.find((item) => item.id === target.value)?.label || ''
    const match = options.value?.gpus.find((item) => label.includes(item.label))
    if (match) gpu.value = match.id
  }
  fitCount()
}

async function loadTargets(preset: LaunchPreset | null = null): Promise<void> {
  const request = ++targetsRequest
  targets.value = []
  target.value = ''
  targetsState.value = 'loading'
  try {
    const list = await api.launchTargets(provider.value, method.value, search.value.trim())
    if (request !== targetsRequest) return
    targets.value = list
    targetsState.value = 'idle'
    if (preset) {
      if (!list.some((item) => item.id === preset.instance.id)) return toast.fail(t('compute.launch.targetGone'))
      target.value = preset.instance.id
    } else target.value = list[0]?.id || ''
    if (method.value === 'startup') syncStartupGpu(preset?.instance)
  } catch (error) {
    if (request !== targetsRequest) return
    targetsState.value = 'failed'
    toast.error(t('common.loadFailed'), error)
  }
}

async function onMethodChange(preset: LaunchPreset | null = null): Promise<void> {
  targetsRequest++
  targets.value = []
  target.value = ''
  targetsState.value = 'idle'
  fitCount()
  // 可搜索的开机方式（镜像 / Pod）等用户输入关键字后再查。
  if (currentMethod.value && !currentMethod.value.searchable) await loadTargets(preset)
}

async function loadOptions(preset: LaunchPreset | null = null): Promise<void> {
  const request = ++optionsRequest
  options.value = null
  method.value = ''
  gpu.value = ''
  count.value = null
  try {
    const loaded = await api.launchOptions(provider.value)
    if (request !== optionsRequest) return
    options.value = loaded
    method.value = preset ? 'startup' : loaded.methods[0]?.id || ''
    gpu.value = loaded.gpus[0]?.id || ''
    count.value = loaded.gpu_counts[0] ?? null
    await onMethodChange(preset)
  } catch (error) {
    if (request === optionsRequest) toast.error(t('compute.launch.optionsFailed'), error)
  }
}

watch(launchOpen, (open) => {
  if (!open) {
    // 作废还没返回的请求。
    optionsRequest++
    targetsRequest++
    return
  }
  if (!providers.value.length) {
    toast.fail(t('compute.launch.noProvider'))
    launchOpen.value = false
    providersOpen.value = true
    return
  }
  const preset = launchPreset.value
  provider.value = preset?.provider || providers.value[0]!.value
  search.value = ''
  services.value = ['asr']
  mode.value = 'always'
  void loadOptions(preset)
})

async function submit(): Promise<void> {
  const chosen = currentMethod.value
  if (!options.value || !chosen) return toast.fail(t('compute.launch.waitOptions'))
  if (!target.value) return toast.fail(t('compute.launch.selectTarget', { target: chosen.target_label }))
  if (!fixedGpu.value) {
    if (!gpu.value) return toast.fail(t('compute.launch.selectGpu'))
    if (count.value == null) return toast.fail(t('compute.launch.selectCount'))
    if (offer.value?.configurations?.length && gpuConfiguration(offer.value, count.value)?.gpu_count !== count.value) {
      return toast.fail(t('compute.launch.countUnsupported'))
    }
  }
  const seconds = interval.value
  if (seconds == null || seconds < 0.5 || seconds > 300) return toast.fail(t('compute.launch.intervalRange'))
  submitting.value = true
  try {
    await api.createLaunch({
      provider: provider.value, method: method.value, target_id: target.value,
      target_label: targets.value.find((item) => item.id === target.value)?.label || target.value,
      gpu_id: fixedGpu.value ? 'existing' : gpu.value,
      gpu_label: fixedGpu.value ? t('compute.launch.keepConfigShort') : offer.value?.label || gpu.value,
      gpu_count: fixedGpu.value ? 1 : count.value || 1,
      interval_seconds: seconds, services: services.value,
      shutdown_mode: canShutdown.value ? mode.value : 'off',
    })
    launchOpen.value = false
    toast.ok(t('compute.launch.started'))
    await refreshStatus()
  } catch (error) {
    toast.error(t('compute.launch.startFailed'), error)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <NModal v-model:show="launchOpen" preset="card" class="modal-card" :title="t('compute.launch.title')">
    <div class="field-row">
      <label class="dc-option"><span>{{ t('cloud.platform') }}<span class="required-mark" :title="t('common.required')" :aria-label="t('common.required')">*</span></span><NSelect v-model:value="provider" filterable :options="providers" @update:value="loadOptions()" /></label>
      <label class="dc-option"><span>{{ t('compute.launch.method') }}<span class="required-mark" :title="t('common.required')" :aria-label="t('common.required')">*</span></span>
        <NSelect v-model:value="method" :options="methodOptions" :loading="!options" :placeholder="options ? t('plugins.fields.select') : t('common.loading')" @update:value="onMethodChange()" />
      </label>
    </div>
    <p v-if="currentMethod?.help" class="field-help" style="margin-bottom: 14px">{{ currentMethod.help }}</p>
    <div v-if="currentMethod?.searchable" class="dc-option">
      <span>{{ t('compute.launch.searchByName') }}<HelpTip :text="t('compute.launch.searchHelp')" /></span>
      <div class="inline-form">
        <NInput v-model:value="search" :placeholder="t('compute.launch.keyword')" @keydown.enter="loadTargets()" />
        <NButton @click="loadTargets()">{{ t('compute.launch.search') }}</NButton>
      </div>
    </div>
    <label class="dc-option"><span>{{ currentMethod?.target_label || t('compute.launch.target') }}<span class="required-mark" :title="t('common.required')" :aria-label="t('common.required')">*</span></span>
      <NSelect
        v-model:value="target" filterable :options="targetOptions" :loading="targetsState === 'loading'" :placeholder="targetPlaceholder"
        @update:value="method === 'startup' && syncStartupGpu()"
      />
    </label>
    <div class="field-row three">
      <label v-if="!fixedGpu" class="dc-option"><span>{{ t('compute.launch.gpuModel') }}<span class="required-mark" :title="t('common.required')" :aria-label="t('common.required')">*</span></span>
        <NSelect v-model:value="gpu" filterable :options="gpuOptions" :placeholder="options ? t('compute.launch.noGpu') : t('common.loading')" @update:value="fitCount" />
      </label>
      <label v-if="!fixedGpu" class="dc-option"><span>{{ t('compute.launch.gpuCount') }}<span class="required-mark" :title="t('common.required')" :aria-label="t('common.required')">*</span></span><NSelect v-model:value="count" :options="countOptions" placeholder="—" /></label>
      <label class="dc-option">{{ t('compute.launch.interval') }}<NInputNumber v-model:value="interval" :min="0.5" :max="300" :step="0.5" /></label>
    </div>
    <p class="field-help" style="margin-bottom: 14px">{{ priceHint }}</p>
    <div class="dc-option">
      <span>{{ t('compute.launch.services') }}<HelpTip :text="t('compute.launch.servicesHelp')" /></span>
      <NSelect v-model:value="services" multiple :options="serviceOptions" :placeholder="t('compute.launch.noServices')" />
    </div>
    <label v-if="services.length && canShutdown" class="dc-option">
      <span>{{ t('compute.afterJobs') }}<HelpTip :text="t('compute.launch.afterJobsHelp')" /></span>
      <NSelect v-model:value="mode" :options="shutdownModeOptions" />
    </label>
    <template #footer>
      <div class="modal-actions">
        <NButton @click="launchOpen = false">{{ t('common.cancel') }}</NButton>
        <NButton type="primary" :disabled="!options?.methods.length" :loading="submitting" @click="submit">{{ t('compute.launch.create') }}</NButton>
      </div>
    </template>
  </NModal>
</template>
