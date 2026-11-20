<script setup lang="ts">
// 一台机器一张卡片：同机多个 Worker 实例合并显示，GPU 指标只显示一次。
import { NButton, NSelect } from 'naive-ui'
import { shutdownModeOptions } from '@/lib/cloud'
import { clampPercent } from '@/lib/format'
import type { Machine } from '@/lib/workers'
import { cloud } from '@/stores/status'
import { bindingMachine } from './dialogs'
import { useMachine } from './machine'
import WorkerTasks from './WorkerTasks.vue'
import { t } from '@/i18n'

const props = defineProps<{ machine: Machine }>()
const { lead, live, multi, binding, device, deviceLabel, tag, state, utilization, memoryFree, capabilities, version, cloudState, instanceText, setMode } = useMachine(props)
</script>

<template>
  <div class="worker" :class="{ offline: !live }">
    <div class="worker-top">
      <div>
        <span class="worker-name">{{ lead.gpu_name || t('compute.machine.unknownDevice') }} · {{ deviceLabel }}</span>
        <div class="worker-gpu" :title="t('compute.machine.keyTitle', { key: machine.key })">{{ tag }}{{ multi ? t('compute.machine.services', { count: machine.members.length }) : '' }} · {{ version.text }}</div>
      </div>
      <span class="pill" :class="state.tone">{{ state.text }}</span>
    </div>

    <div class="worker-stats">
      <div class="worker-stat">
        <span>{{ device === 'cpu' ? t('compute.machine.cpuUsage') : t('compute.machine.gpuUsage') }}</span>
        <b>{{ utilization != null ? `${utilization}%` : '-' }}</b>
        <div v-if="utilization != null" class="meter" :class="{ hot: clampPercent(utilization) >= 60 }">
          <span :style="{ width: `${clampPercent(utilization)}%` }" />
        </div>
      </div>
      <div class="worker-stat">
        <span>{{ device === 'cuda' ? t('compute.machine.freeVram') : t('compute.machine.freeRam') }}</span>
        <b>{{ memoryFree }}</b>
      </div>
    </div>

    <WorkerTasks :machine="machine" />

    <div v-if="binding && cloudState" class="worker-cloud">
      <div class="worker-cloud-head">
        <span>☁ {{ binding.provider_label }} ·
          <span v-if="!binding.instance && binding.instance_error" class="warn-text" :title="binding.instance_error">{{ t('compute.machine.hostFailed') }}</span>
          <template v-else>{{ instanceText }}</template>
        </span>
        <NButton size="small" quaternary @click="bindingMachine = machine.key">{{ t('compute.machine.change') }}</NButton>
      </div>
      <div class="worker-cloud-head">
        <span class="worker-cloud-state" :class="cloudState.tone" :title="cloudState.text">{{ cloudState.text }}{{ cloud?.auto_enabled ? '' : t('compute.machine.previewNoShutdown') }}</span>
        <NSelect v-if="binding.can_shutdown !== false" size="small" :value="binding.shutdown_mode" :options="shutdownModeOptions" :title="t('compute.afterJobs')" @update:value="setMode" />
      </div>
    </div>
    <div v-else-if="cloud?.providers_configured" class="worker-cloud">
      <div class="worker-cloud-head">
        <span class="worker-cloud-state">{{ t('compute.machine.noHost') }}</span>
        <NButton size="small" @click="bindingMachine = machine.key">{{ t('compute.machine.bindHost') }}</NButton>
      </div>
    </div>

    <div class="worker-caps">
      {{ capabilities.asr }}<template v-if="capabilities.restore"> {{ t('compute.machine.restore', { restore: capabilities.restore }) }}</template>
      <template v-if="capabilities.reason"> · <span class="warn-text" :title="capabilities.reason">{{ t('compute.machine.restoreUnavailable', { reason: capabilities.reasonShort }) }}</span></template>
      <template v-if="capabilities.unsupported"> · <span class="warn-text">{{ t('compute.machine.asrUnsupported') }}</span></template>
      <template v-if="version.mismatch"> · <span class="warn-text">{{ version.mismatch }}</span></template>
    </div>
  </div>
</template>
