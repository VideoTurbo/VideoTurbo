<script setup lang="ts">
// Worker 连接主控用的共享密钥：默认遮住，可显示、复制和重置。
import { NButton, NInput } from 'naive-ui'
import { onActivated, onMounted, ref } from 'vue'
import { api } from '@/api'
import AppIcon from '@/components/AppIcon.vue'
import { confirm, toast } from '@/lib/feedback'
import { t } from '@/i18n'

const token = ref('')
const visible = ref(false)

async function load(): Promise<void> {
  visible.value = false
  try {
    token.value = (await api.workerToken()).token
  } catch (error) {
    toast.error(t('settings.token.loadFailed'), error)
  }
}
// 切到这个分类时挂载；从别的页面回到设置页时重新激活。
onMounted(load)
onActivated(load)

async function copy(): Promise<void> {
  try {
    await navigator.clipboard.writeText(token.value)
    toast.ok(t('settings.token.copied'))
  } catch {
    // 非 HTTPS 的局域网地址没有剪贴板接口，只能手动复制。
    visible.value = true
    toast.fail(t('settings.token.copyBlocked'))
  }
}

async function reset(): Promise<void> {
  if (!await confirm(t('settings.token.resetConfirm'),
    { title: t('settings.token.resetTitle'), confirmLabel: t('settings.token.resetButton'), danger: true })) return
  try {
    token.value = (await api.resetWorkerToken()).token
    visible.value = true
    toast.ok(t('settings.token.reset'))
  } catch (error) {
    toast.error(t('settings.token.resetFailed'), error)
  }
}
</script>

<template>
  <div class="settings-section">
    <h3>{{ t('settings.token.title') }}</h3>
    <p class="lead">{{ t('settings.token.help') }}</p>
    <div class="inline-form secret-field">
      <NInput :value="token" readonly :type="visible ? 'text' : 'password'" :aria-label="t('settings.token.title')" />
      <NButton @click="visible = !visible"><template #icon><AppIcon name="eye" /></template>{{ visible ? t('settings.token.hide') : t('settings.token.show') }}</NButton>
      <NButton @click="copy"><template #icon><AppIcon name="copy" /></template>{{ t('settings.token.copy') }}</NButton>
      <NButton type="error" secondary @click="reset"><template #icon><AppIcon name="refresh" /></template>{{ t('settings.token.resetShort') }}</NButton>
    </div>
    <span class="field-help">{{ t('settings.token.resetHelp') }}</span>
  </div>
</template>
