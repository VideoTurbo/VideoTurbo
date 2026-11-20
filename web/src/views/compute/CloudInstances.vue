<script setup lang="ts">
// 各云平台账号下的实例。抢卡、开机、部署都体现为实例的状态和部署情况；还在抢卡的实例也占一项。
import { NButton } from 'naive-ui'
import { computed } from 'vue'
import AppIcon from '@/components/AppIcon.vue'
import ViewToggle from '@/components/ViewToggle.vue'
import { instanceCard, instanceEntries } from '@/lib/cloud'
import { clockSeconds } from '@/lib/format'
import { useViewMode } from '@/lib/viewMode'
import { instanceGroups, instancesAt, refreshInstances } from '@/stores/cloud'
import { cloud, tick, titleMachine } from '@/stores/status'
import { launchLogId, providersOpen } from './dialogs'
import InstanceActions from './InstanceActions.vue'
import { t } from '@/i18n'

const view = useViewMode('instances')
const errors = computed(() => instanceGroups.value.filter((group) => group.error))
const providerLabel = (id: string) => cloud.value?.launch_providers.find((provider) => provider.id === id)?.label || ''
const cards = computed(() => {
  void tick.value // 已运行、已用时间跟随状态轮询更新
  return instanceEntries(instanceGroups.value, cloud.value?.launches || [], providerLabel, instancesAt.value > 0)
    .map((entry) => instanceCard(entry, titleMachine))
})
</script>

<template>
  <div class="section-head">
    <h3>{{ t('compute.instances.title') }}</h3>
    <span class="section-sub">{{ instancesAt ? t('compute.instances.updatedAt', { time: clockSeconds(instancesAt) }) : t('compute.instances.allAccounts') }}{{ t('compute.instances.rentHint') }}</span>
    <span class="spacer" />
    <NButton v-if="cloud?.providers_configured" size="small" @click="refreshInstances()">
      <template #icon><AppIcon name="refresh" /></template>{{ t('compute.instances.refresh') }}
    </NButton>
    <ViewToggle v-if="cloud?.providers_configured" v-model="view" />
  </div>

  <div v-if="!cloud?.providers_configured" class="empty-card">
    <b>{{ t('compute.instances.noPlatform') }}</b>
    <span>{{ t('compute.instances.noPlatformHelp') }}</span>
    <NButton type="primary" @click="providersOpen = true"><template #icon><AppIcon name="plus" /></template>{{ t('compute.instances.connect') }}</NButton>
  </div>
  <template v-else>
    <div v-for="group in errors" :key="group.provider" class="warning bad">{{ t('common.labelError', { label: group.label, error: group.error }) }}</div>

    <div v-if="cards.length && view === 'card'" class="instance-grid">
      <div v-for="card in cards" :key="card.key" class="instance">
        <div class="instance-top">
          <div class="instance-name"><b>{{ card.title }}</b><span class="path" :title="card.instance ? t('compute.instances.col.id') : t('compute.instances.col.method')">{{ card.sub }}</span></div>
          <span class="pill" :class="card.tone">{{ card.statusText }}</span>
        </div>
        <dl class="instance-meta">
          <div v-for="[name, value] in card.meta" :key="name"><dt>{{ name }}</dt><dd :title="value">{{ value }}</dd></div>
        </dl>
        <button v-if="card.deploy" type="button" class="instance-deploy" :title="t('compute.instances.viewLog')" @click="launchLogId = card.launch!.id">
          <span class="instance-deploy-text" :class="card.deploy.tone">{{ card.deploy.text }}</span>
          <span v-if="card.deploy.detail" class="instance-deploy-detail" :title="card.deploy.detail">{{ card.deploy.detail }}</span>
        </button>
        <div class="instance-actions">
          <span v-if="card.hint" class="muted">{{ card.hint }}</span>
          <span class="spacer" />
          <InstanceActions :card="card" />
        </div>
      </div>
    </div>

    <div v-else-if="cards.length" class="card table-wrap">
      <table class="jobs-table compute-table">
        <thead>
          <tr><th>{{ t('compute.instances.col.host') }}</th><th>{{ t('list.col.status') }}</th><th>{{ t('compute.instances.col.platform') }}</th><th>GPU</th><th>{{ t('compute.instances.col.region') }}</th><th>{{ t('compute.instances.col.uptime') }}</th><th>{{ t('compute.instances.col.machine') }}</th><th>{{ t('compute.instances.col.services') }}</th><th class="col-actions">{{ t('list.col.actions') }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="card in cards" :key="card.key">
            <td><div class="instance-name"><b>{{ card.title }}</b><span class="path" :title="card.instance ? t('compute.instances.col.id') : t('compute.instances.col.method')">{{ card.sub }}</span></div></td>
            <td><span class="pill" :class="card.tone">{{ card.statusText }}</span></td>
            <td>{{ card.platform }}</td>
            <td>{{ card.gpu }}</td>
            <td>{{ card.region }}</td>
            <td>{{ card.uptime }}</td>
            <td>{{ card.machine }}</td>
            <td class="col-deploy">
              <button v-if="card.deploy" type="button" class="instance-deploy" :title="t('compute.instances.viewLog')" @click="launchLogId = card.launch!.id">
                <span class="instance-deploy-text" :class="card.deploy.tone">{{ card.deploy.text }}</span>
                <span v-if="card.deploy.detail" class="instance-deploy-detail" :title="card.deploy.detail">{{ card.deploy.detail }}</span>
              </button>
              <span v-else class="muted">{{ card.hint || '—' }}</span>
            </td>
            <td class="col-actions"><div class="row-actions"><InstanceActions :card="card" /></div></td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-else-if="!errors.length && instancesAt" class="empty-card">
      <b>{{ t('compute.noHosts') }}</b><span>{{ t('compute.instances.emptyHelp') }}</span>
    </div>
  </template>
</template>
