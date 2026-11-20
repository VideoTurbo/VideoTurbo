<script setup lang="ts">
// 云主机：已注册的机器和云实例（创建任务并入实例显示）。
import { NButton } from 'naive-ui'
import { computed, onActivated } from 'vue'
import AppIcon from '@/components/AppIcon.vue'
import ViewToggle from '@/components/ViewToggle.vue'
import { useViewMode } from '@/lib/viewMode'
import { groupMachines, workerOnline } from '@/lib/workers'
import { refreshInstances } from '@/stores/cloud'
import { cloud, heartbeatTimeout, status } from '@/stores/status'
import CloudBindDialog from './CloudBindDialog.vue'
import CloudInstances from './CloudInstances.vue'
import CloudProvidersDialog from './CloudProvidersDialog.vue'
import { openLaunch, providersOpen } from './dialogs'
import LaunchDialog from './LaunchDialog.vue'
import LaunchLogDialog from './LaunchLogDialog.vue'
import WorkerCard from './WorkerCard.vue'
import WorkerRow from './WorkerRow.vue'
import { t } from '@/i18n'

const machineView = useViewMode('machines')
const workers = computed(() => status.value?.workers || [])
const machines = computed(() => groupMachines(workers.value, heartbeatTimeout.value))
const providerCount = computed(() => (cloud.value?.providers_configured ? cloud.value.launch_providers.length : 0))

const summary = computed(() => {
  if (!status.value) return t('common.loading')
  const list = machines.value
  if (!list.length) return t('compute.noMachines')
  const busy = list.reduce((total, machine) => total + machine.busyCount, 0)
  const restoring = workers.value.filter((w) => w.task_kind === 'restore' && w.current_job_id && workerOnline(w, heartbeatTimeout.value)).length
  return t('compute.summary', { count: list.length, online: list.filter((machine) => machine.onlineCount).length })
    + (busy ? t('compute.busy', { count: busy }) + (restoring ? t('compute.restoring', { count: restoring }) : '') : t('compute.idle'))
})
const offlineTip = computed(() => t('compute.offlineAfter', { value: heartbeatTimeout.value }))

onActivated(() => {
  if (cloud.value?.providers_configured) void refreshInstances()
})
</script>

<template>
  <section class="pane">
    <div class="page-head">
      <div class="page-title"><h2>{{ t('compute.title') }}</h2><p class="page-sub" :title="offlineTip">{{ summary }}</p></div>
      <div class="page-actions">
        <NButton @click="providersOpen = true">
          <template #icon><AppIcon name="cloud" /></template>{{ t('cloud.platform') }}<span v-if="providerCount" class="btn-count" style="margin-left: 6px">{{ providerCount }}</span>
        </NButton>
        <NButton type="primary" @click="openLaunch()"><template #icon><AppIcon name="plus" /></template>{{ t('compute.create') }}</NButton>
      </div>
    </div>

    <div class="section-head">
      <h3>{{ t('compute.machines') }}</h3><span class="section-sub">{{ t('compute.machinesHelp') }}</span>
      <span class="spacer" />
      <ViewToggle v-if="machines.length" v-model="machineView" />
    </div>
    <div v-if="machines.length && machineView === 'list'" class="card table-wrap">
      <table class="jobs-table compute-table">
        <thead>
          <tr><th>{{ t('compute.col.machine') }}</th><th>{{ t('list.col.status') }}</th><th>{{ t('compute.col.usage') }}</th><th>{{ t('compute.col.memory') }}</th><th>{{ t('compute.col.task') }}</th><th>{{ t('compute.col.host') }}</th><th>{{ t('compute.col.capabilities') }}</th><th class="col-actions">{{ t('list.col.actions') }}</th></tr>
        </thead>
        <tbody><WorkerRow v-for="machine in machines" :key="machine.key" :machine="machine" /></tbody>
      </table>
    </div>
    <div v-else class="worker-grid">
      <WorkerCard v-for="machine in machines" :key="machine.key" :machine="machine" />
      <div v-if="!machines.length" class="empty">{{ t('compute.noMachines') }}</div>
    </div>

    <div class="cloud-area">
      <CloudInstances />
    </div>

    <CloudBindDialog />
    <CloudProvidersDialog />
    <LaunchDialog />
    <LaunchLogDialog />
  </section>
</template>
