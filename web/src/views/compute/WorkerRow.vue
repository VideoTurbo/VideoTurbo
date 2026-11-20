<script setup lang="ts">
// 列表视图里的一台机器：内容与卡片一致，排成一行。
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
  <tr :class="{ offline: !live }">
    <td>
      <div class="instance-name">
        <b>{{ lead.gpu_name || t('compute.machine.unknownDevice') }} · {{ deviceLabel }}</b>
        <span class="path" :title="t('compute.machine.keyTitle', { key: machine.key })">{{ tag }}{{ multi ? t('compute.machine.services', { count: machine.members.length }) : '' }} · {{ version.text }}</span>
      </div>
    </td>
    <td><span class="pill" :class="state.tone">{{ state.text }}</span></td>
    <td class="col-meter" :title="device === 'cpu' ? t('compute.machine.cpuUsage') : t('compute.machine.gpuUsage')">
      <template v-if="utilization != null">
        {{ utilization }}%
        <div class="meter" :class="{ hot: clampPercent(utilization) >= 60 }"><span :style="{ width: `${clampPercent(utilization)}%` }" /></div>
      </template>
      <template v-else>-</template>
    </td>
    <td :title="device === 'cuda' ? t('compute.machine.freeVram') : t('compute.machine.freeRam')">{{ memoryFree }}</td>
    <td class="col-tasks"><WorkerTasks :machine="machine" /></td>
    <td class="col-cloud">
      <template v-if="binding && cloudState">
        <div>☁ {{ binding.provider_label }} ·
          <span v-if="!binding.instance && binding.instance_error" class="warn-text" :title="binding.instance_error">{{ t('compute.machine.hostFailed') }}</span>
          <template v-else>{{ instanceText }}</template>
        </div>
        <div class="worker-cloud-state" :class="cloudState.tone" :title="cloudState.text">{{ cloudState.text }}{{ cloud?.auto_enabled ? '' : t('compute.machine.previewNoShutdown') }}</div>
      </template>
      <span v-else-if="cloud?.providers_configured" class="muted">{{ t('compute.machine.noHost') }}</span>
      <span v-else class="muted">—</span>
    </td>
    <td class="col-caps">
      {{ capabilities.asr }}<template v-if="capabilities.restore"> {{ t('compute.machine.restore', { restore: capabilities.restore }) }}</template>
      <template v-if="capabilities.reason"> · <span class="warn-text" :title="capabilities.reason">{{ t('compute.machine.restoreUnavailable', { reason: capabilities.reasonShort }) }}</span></template>
      <template v-if="capabilities.unsupported"> · <span class="warn-text">{{ t('compute.machine.asrUnsupported') }}</span></template>
      <template v-if="version.mismatch"> · <span class="warn-text">{{ version.mismatch }}</span></template>
    </td>
    <td class="col-actions">
      <div v-if="cloud?.providers_configured" class="row-actions">
        <NSelect v-if="binding && binding.can_shutdown !== false" size="small" :value="binding.shutdown_mode" :options="shutdownModeOptions" :title="t('compute.afterJobs')" @update:value="setMode" />
        <NButton size="small" :quaternary="!!binding" @click="bindingMachine = machine.key">{{ binding ? t('compute.machine.change') : t('compute.machine.bindHost') }}</NButton>
      </div>
    </td>
  </tr>
</template>
