<script setup lang="ts">
// 把一台机器绑定到云实例，绑定后任务跑完可以自动关机。
import { NButton, NModal, NRadio, NSelect } from 'naive-ui'
import { computed, ref, watch } from 'vue'
import { api, errorMessage, type CloudInstanceGroup, type ShutdownMode } from '@/api'
import { shutdownModeOptions } from '@/lib/cloud'
import { confirm, toast } from '@/lib/feedback'
import { duration } from '@/lib/format'
import { fetchInstances, refreshInstances } from '@/stores/cloud'
import { cloud, refreshStatus, titleMachine } from '@/stores/status'
import { bindingMachine } from './dialogs'
import { t } from '@/i18n'

const groups = ref<CloudInstanceGroup[] | null>(null)
const error = ref('')
/** 选中的实例，格式为 “平台|实例 ID”。 */
const picked = ref('')
const mode = ref<ShutdownMode>('always')
const saving = ref(false)

const machine = computed(() => bindingMachine.value)
const current = computed(() => (machine.value ? cloud.value?.bindings?.[machine.value] : undefined))

const options = computed(() => (groups.value || []).flatMap((group) => group.instances.map((instance) => {
  const taken = !!instance.machine && instance.machine !== machine.value
  const meta = [
    group.label, (instance.gpu_name || t('compute.bind.unknownGpu')) + (instance.gpu_count > 1 ? ` ×${instance.gpu_count}` : ''), instance.status_text,
    instance.status === 'running' && instance.started_at ? t('compute.bind.running', { time: duration(Date.now() / 1000 - instance.started_at) }) : '',
    taken ? t('compute.bind.boundTo', { machine: instance.machine }) : '',
  ].filter(Boolean).join(' · ')
  return { value: `${group.provider}|${instance.id}`, title: instance.title || instance.id.slice(0, 8), id: instance.id, meta, taken }
})))

watch(machine, async (key) => {
  if (!key) return
  groups.value = null
  error.value = ''
  mode.value = current.value?.shutdown_mode || 'always'
  picked.value = current.value ? `${current.value.provider}|${current.value.instance_id}` : ''
  try {
    const loaded = await fetchInstances()
    if (machine.value !== key) return
    groups.value = loaded
    // 没有绑定过且只有一台空闲的运行中实例时，直接选中它。
    const free = loaded.flatMap((group) => group.instances.filter((i) => i.status === 'running' && !i.machine).map((i) => `${group.provider}|${i.id}`))
    if (!picked.value && free.length === 1) picked.value = free[0]!
  } catch (cause) {
    error.value = errorMessage(cause)
  }
})

async function finish(action: () => Promise<unknown>, done: string, failed: string): Promise<void> {
  saving.value = true
  try {
    await action()
    bindingMachine.value = null
    toast.ok(done)
    await refreshStatus()
    await refreshInstances()
  } catch (cause) {
    toast.error(failed, cause)
  } finally {
    saving.value = false
  }
}

function bind(): Promise<void> | void {
  const key = machine.value
  if (!key) return
  if (!picked.value) return toast.fail(t('compute.bind.selectFirst'))
  const [provider, ...rest] = picked.value.split('|')
  return finish(() => api.bindCloud(key, { provider: provider!, instance_id: rest.join('|'), shutdown_mode: mode.value }), t('compute.bind.bound', { key }), t('compute.bind.bindFailed'))
}

async function unbind(): Promise<void> {
  const key = machine.value
  if (!key) return
  if (await confirm(t('compute.bind.unbindConfirm', { key }), { title: t('compute.bind.unbindTitle'), confirmLabel: t('compute.bind.unbind') })) {
    await finish(() => api.unbindCloud(key), t('compute.bind.unbound'), t('compute.bind.unbindFailed'))
  }
}
</script>

<template>
  <NModal :show="machine !== null" preset="card" class="modal-card" :title="t('compute.bind.title', { machine: machine ? titleMachine(machine) : '' })" @update:show="bindingMachine = null">
    <p class="lead">{{ t('compute.bind.help') }}</p>
    <div class="option-list">
      <div v-if="error" class="warning bad">{{ t('compute.bind.loadFailed', { error }) }}</div>
      <div v-else-if="!groups" class="muted">{{ t('compute.bind.loading') }}</div>
      <template v-else>
        <div v-for="group in groups.filter((g) => g.error)" :key="group.provider" class="warning bad">{{ t('common.labelError', { label: group.label, error: group.error }) }}</div>
        <label v-for="option in options" :key="option.value" class="option" :class="{ disabled: option.taken, checked: picked === option.value }">
          <NRadio :checked="picked === option.value" :disabled="option.taken" name="cloud-bind" @update:checked="picked = option.value" />
          <span><b>{{ option.title }}</b><span class="option-meta">{{ option.meta }}</span><span class="option-meta path">{{ option.id }}</span></span>
        </label>
        <div v-if="!options.length && !groups.some((g) => g.error)" class="empty">{{ t('compute.noHosts') }}</div>
      </template>
    </div>
    <label class="dc-option">{{ t('compute.afterJobs') }}<NSelect v-model:value="mode" :options="shutdownModeOptions" /></label>
    <template #footer>
      <div class="modal-actions">
        <NButton v-if="current" type="error" secondary :disabled="saving" @click="unbind">{{ t('compute.bind.unbind') }}</NButton>
        <span class="spacer" />
        <NButton @click="bindingMachine = null">{{ t('common.cancel') }}</NButton>
        <NButton type="primary" :loading="saving" @click="bind">{{ t('compute.bind.bind') }}</NButton>
      </div>
    </template>
  </NModal>
</template>
