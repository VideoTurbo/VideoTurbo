<script setup lang="ts">
// 一台云实例的操作：最该做的一个直接显示成按钮，其余收进“更多”菜单。卡片和列表视图共用。
import { NButton } from 'naive-ui'
import { computed, ref } from 'vue'
import { api } from '@/api'
import type { IconName } from '@/components/icons'
import type { MenuItem } from '@/components/menu'
import MoreMenu from '@/components/MoreMenu.vue'
import { instanceActionLabel, type InstanceAction, type InstanceCard } from '@/lib/cloud'
import { confirm, toast } from '@/lib/feedback'
import { refreshInstances } from '@/stores/cloud'
import { refreshStatus } from '@/stores/status'
import { launchLogId, openLaunch } from './dialogs'
import { t } from '@/i18n'

const props = defineProps<{ card: InstanceCard }>()
/** 正在提交操作，期间禁用主按钮，避免重复点击。 */
const working = ref(false)

async function run(action: () => Promise<unknown>, done: string, failed: string): Promise<void> {
  if (working.value) return
  working.value = true
  try {
    await action()
    if (done) toast.ok(done)
    await refreshStatus()
    await refreshInstances()
  } catch (error) {
    toast.error(failed, error)
  } finally {
    working.value = false
  }
}

const instanceAction = (action: 'shutdown' | 'destroy' | 'forget') =>
  api.cloudInstanceAction(props.card.provider, props.card.instance!.id, action)

async function stop(): Promise<void> {
  const launch = props.card.launch!
  const billing = launch.instance_id && launch.state !== 'grabbing' ? t('compute.instance.billingWarning') : ''
  const label = instanceActionLabel(props.card, 'stop')
  if (await confirm(t('compute.instance.stopConfirm', { label, billing }), { title: t('compute.instance.stopTitle', { label }), confirmLabel: label, danger: true })) {
    await run(() => api.cancelLaunch(launch.id), t('compute.instance.stopped'), t('compute.instance.stopFailed'))
  }
}

async function redeploy(forgetHostKey = false): Promise<void> {
  const ok = forgetHostKey
    ? await confirm(t('compute.instance.trustConfirm'),
      { title: t('compute.instance.trustTitle'), confirmLabel: t('compute.instance.trustButton'), danger: true })
    : await confirm(t('compute.instance.redeployConfirm'),
      { title: t('compute.instance.redeployTitle'), confirmLabel: t('compute.instance.redeploy'), danger: true })
  if (ok) await run(() => api.redeployLaunch(props.card.launch!.id, forgetHostKey), t('compute.instance.redeployStarted'), t('compute.instance.redeployFailed'))
}

async function shutdown(): Promise<void> {
  const machine = props.card.instance!.machine
  const warn = machine ? t('compute.instance.shutdownJobs', { machine }) : ''
  if (await confirm(t('compute.instance.shutdownConfirm', { title: props.card.title, warn }), { title: t('compute.instance.shutdownTitle'), confirmLabel: t('compute.instance.shutdownButton'), danger: true })) {
    await run(() => instanceAction('shutdown'), t('compute.instance.shutdownSent'), t('compute.instance.shutdownFailed'))
  }
}

async function destroy(): Promise<void> {
  if (await confirm(t('compute.instance.destroyConfirm', { title: props.card.title }),
    { title: t('compute.instance.destroyTitle'), confirmLabel: t('compute.instance.destroyButton'), danger: true })) {
    await run(() => instanceAction('destroy'), t('compute.instance.destroySent'), t('compute.instance.destroyFailed'))
  }
}

async function forget(): Promise<void> {
  if (await confirm(t('compute.instance.forgetConfirm', { title: props.card.title }),
    { title: t('compute.instance.forgetTitle'), confirmLabel: t('compute.instance.forgetButton'), danger: true })) {
    await run(() => instanceAction('forget'), t('compute.instance.forgotten'), t('compute.instance.forgetFailed'))
  }
}

const handlers: Record<InstanceAction, () => unknown> = {
  stop,
  continueAsr: () => run(() => api.continueLaunchAsr(props.card.launch!.id), t('compute.instance.continueAsr'), t('compute.instance.continueFailed')),
  redeploy: () => redeploy(),
  trustHostKey: () => redeploy(true),
  log: () => { launchLogId.value = props.card.launch!.id },
  shutdown,
  start: () => openLaunch({ provider: props.card.provider, instance: props.card.instance! }),
  destroy,
  forget,
  clear: () => run(() => api.deleteLaunch(props.card.launch!.id), '', t('compute.instance.clearFailed')),
}

const icons: Record<InstanceAction, IconName> = {
  stop: 'pause', continueAsr: 'play', redeploy: 'rotate', trustHostKey: 'rotate', log: 'list',
  shutdown: 'pause', start: 'play', destroy: 'trash', forget: 'x', clear: 'x',
}
const danger = new Set<InstanceAction>(['stop', 'trustHostKey', 'shutdown', 'destroy'])
/** 主按钮的样式：危险操作用红色，开机用绿色，补救部署用主色。 */
const primaryStyle = computed(() => {
  const action = props.card.primary
  if (!action) return {}
  if (action === 'start') return { type: 'success' as const, secondary: true }
  if (action === 'continueAsr') return { type: 'primary' as const }
  return danger.has(action) ? { type: 'error' as const, secondary: true } : {}
})

const menu = computed<MenuItem[]>(() => props.card.menu.map((action) => (action === '-' ? '-' : {
  label: instanceActionLabel(props.card, action), icon: icons[action], danger: danger.has(action), run: handlers[action],
})))
</script>

<template>
  <NButton v-if="card.primary" size="small" v-bind="primaryStyle" :disabled="working" @click="handlers[card.primary]()">
    {{ instanceActionLabel(card, card.primary) }}
  </NButton>
  <MoreMenu :items="menu" />
</template>
