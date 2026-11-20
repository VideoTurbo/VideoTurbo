<script setup lang="ts">
// 控制台的管理口令与退出登录。
import { NButton, NInput } from 'naive-ui'
import { reactive, ref } from 'vue'
import { api } from '@/api'
import FormField from '@/components/FormField.vue'
import { toast } from '@/lib/feedback'
import { t } from '@/i18n'

const form = reactive({ current: '', next: '', confirm: '' })
const saving = ref(false)

async function change(): Promise<void> {
  if (form.next.length < 8) return toast.fail(t('settings.account.tooShort'))
  if (form.next !== form.confirm) return toast.fail(t('settings.account.mismatch'))
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

async function logout(): Promise<void> {
  try {
    await api.logout()
  } finally {
    location.href = '/login'
  }
}
</script>

<template>
  <div class="settings-section">
    <h3>{{ t('settings.account.title') }}</h3>
    <FormField :label="t('settings.account.current')" required><NInput v-model:value="form.current" type="password" :input-props="{ autocomplete: 'current-password' }" /></FormField>
    <FormField :label="t('settings.account.new')" required :help="t('settings.account.newHelp')"><NInput v-model:value="form.next" type="password" :input-props="{ autocomplete: 'new-password' }" /></FormField>
    <FormField :label="t('settings.account.repeat')" required><NInput v-model:value="form.confirm" type="password" :input-props="{ autocomplete: 'new-password' }" /></FormField>
    <div style="display: flex; gap: 8px">
      <NButton type="primary" :loading="saving" @click="change">{{ t('settings.account.change') }}</NButton>
      <NButton @click="logout">{{ t('settings.account.logout') }}</NButton>
    </div>
  </div>
</template>
