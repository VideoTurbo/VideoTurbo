<script setup lang="ts">
// 控制台的管理口令与退出登录。桌面版应用内窗口不需要口令：还没设定时在这里设定，供其他设备登录。
import { NButton, NInput } from 'naive-ui'
import { onMounted, reactive, ref } from 'vue'
import { api, type AuthStatus } from '@/api'
import FormField from '@/components/FormField.vue'
import { toast } from '@/lib/feedback'
import { t } from '@/i18n'

const form = reactive({ current: '', next: '', confirm: '' })
const saving = ref(false)
const auth = ref<AuthStatus | null>(null)

async function loadStatus(): Promise<void> {
  try {
    auth.value = await api.authStatus()
  } catch {
    // 读不到时按已设定口令显示修改表单，与原来一致。
  }
}

function checkNew(): boolean {
  if (form.next.length < 8) {
    toast.fail(t('settings.account.tooShort'))
    return false
  }
  if (form.next !== form.confirm) {
    toast.fail(t('settings.account.mismatch'))
    return false
  }
  return true
}

async function change(): Promise<void> {
  if (!checkNew()) return
  saving.value = true
  try {
    await api.changePassword(form.current, form.next)
    Object.assign(form, { current: '', next: '', confirm: '' })
    toast.ok(t('settings.account.changed'))
  } catch (error) {
    toast.error(t('settings.account.changeFailed'), error)
  } finally {
    saving.value = false
  }
}

async function setup(): Promise<void> {
  if (!checkNew()) return
  saving.value = true
  try {
    await api.setupPassword(form.next)
    Object.assign(form, { current: '', next: '', confirm: '' })
    toast.ok(t('settings.account.setupDone'))
    await loadStatus()
  } catch (error) {
    toast.error(t('settings.account.setupFailed'), error)
  } finally {
    saving.value = false
  }
}

async function logout(): Promise<void> {
  try {
    await api.logout()
  } finally {
    location.href = '/login'
  }
}

onMounted(loadStatus)
</script>

<template>
  <div class="settings-section">
    <h3>{{ t('settings.account.title') }}</h3>
    <template v-if="auth && !auth.configured">
      <p class="field-help">{{ t('settings.account.noPassword') }}</p>
      <FormField :label="t('settings.account.new')" required :help="t('settings.account.newHelp')"><NInput v-model:value="form.next" type="password" :input-props="{ autocomplete: 'new-password' }" /></FormField>
      <FormField :label="t('settings.account.repeat')" required><NInput v-model:value="form.confirm" type="password" :input-props="{ autocomplete: 'new-password' }" /></FormField>
      <NButton type="primary" :loading="saving" @click="setup">{{ t('settings.account.setup') }}</NButton>
    </template>
    <template v-else>
      <p v-if="auth?.desktop_session" class="field-help">{{ t('settings.account.desktopHelp') }}</p>
      <FormField :label="t('settings.account.current')" required><NInput v-model:value="form.current" type="password" :input-props="{ autocomplete: 'current-password' }" /></FormField>
      <FormField :label="t('settings.account.new')" required :help="t('settings.account.newHelp')"><NInput v-model:value="form.next" type="password" :input-props="{ autocomplete: 'new-password' }" /></FormField>
      <FormField :label="t('settings.account.repeat')" required><NInput v-model:value="form.confirm" type="password" :input-props="{ autocomplete: 'new-password' }" /></FormField>
      <div style="display: flex; gap: 8px">
        <NButton type="primary" :loading="saving" @click="change">{{ t('settings.account.change') }}</NButton>
        <NButton v-if="!auth?.desktop_session" @click="logout">{{ t('settings.account.logout') }}</NButton>
      </div>
    </template>
  </div>
</template>
