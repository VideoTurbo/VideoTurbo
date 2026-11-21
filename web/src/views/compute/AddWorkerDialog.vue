<script setup lang="ts">
// 添加显卡电脑：选好其他电脑能访问到的接入地址，生成接入串（地址 + 接入密钥），在显卡电脑上粘贴一次即可连上。
import { NButton, NInput, NModal, NSelect } from 'naive-ui'
import { computed, ref, watch } from 'vue'
import { api, errorMessage } from '@/api'
import AppIcon from '@/components/AppIcon.vue'
import { toast } from '@/lib/feedback'
import { isDesktop } from '@/stores/status'
import { addWorkerOpen } from './dialogs'
import { t } from '@/i18n'

const LOOPBACK = new Set(['localhost', '127.0.0.1', '::1', '[::1]'])

const urls = ref<string[]>([])
const url = ref<string | null>(null)
const code = ref('')
const error = ref('')
const loading = ref(false)
let request = 0
/** 打开弹窗时自己生成一次；这期间地址变化不再触发生成。 */
let opening = false

const options = computed(() => urls.value.map((value) => ({ label: value, value })))

watch(addWorkerOpen, async (open) => {
  if (!open) return
  code.value = ''
  error.value = ''
  opening = true
  try {
    const info = await api.workerConnection()
    // 浏览器打开控制台用的地址，其他电脑多半也能访问；桌面版在本机打开时是 127.0.0.1，改用网卡地址。
    const hosts = [location.hostname, ...info.addresses].filter((host) => host && !LOOPBACK.has(host))
    urls.value = [...new Set(hosts.map((host) => `http://${host.includes(':') ? `[${host}]` : host}:${info.worker_port}`))]
    // 再次打开时地址和上次一样，watch(url) 不会触发，所以这里直接生成。
    url.value = urls.value[0] ?? null
    if (url.value) void generate(url.value)
    else error.value = t('compute.addWorker.noAddress')
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    opening = false
  }
})

watch(url, (value) => {
  if (!opening) void generate(value)
})

async function generate(value: string | null): Promise<void> {
  code.value = ''
  if (!value) return
  const current = ++request
  loading.value = true
  try {
    const result = await api.createWorkerConnection(value)
    if (current === request) {
      code.value = result.code
      error.value = ''
    }
  } catch (cause) {
    if (current === request) error.value = errorMessage(cause)
  } finally {
    if (current === request) loading.value = false
  }
}

async function copy(): Promise<void> {
  try {
    await navigator.clipboard.writeText(code.value)
    toast.ok(t('compute.addWorker.copied'))
  } catch {
    // 非 HTTPS 的局域网地址没有剪贴板接口：选中文字，让用户自己复制。
    const field = document.querySelector<HTMLTextAreaElement>('.connection-code textarea')
    field?.select()
    toast.fail(t('compute.addWorker.copyBlocked'))
  }
}
</script>

<template>
  <NModal :show="addWorkerOpen" preset="card" class="modal-card" :title="t('compute.addWorker.title')" @update:show="addWorkerOpen = $event">
    <ol class="lead add-worker-steps">
      <li>{{ t('compute.addWorker.step1') }}</li>
      <li>{{ t('compute.addWorker.step2') }}</li>
    </ol>
    <p v-if="isDesktop" class="field-help" style="margin: -6px 0 14px">{{ t('compute.addWorker.localHint') }}</p>
    <label class="field-label">{{ t('compute.addWorker.address') }}</label>
    <NSelect v-model:value="url" :options="options" filterable tag :placeholder="t('compute.addWorker.addressPlaceholder')" />
    <span class="field-help">{{ t('compute.addWorker.addressHelp') }}</span>
    <label class="field-label" style="margin-top: 14px">{{ t('compute.addWorker.code') }}</label>
    <NInput class="connection-code secret-field" type="textarea" :value="code" readonly :autosize="{ minRows: 2, maxRows: 4 }" :loading="loading" :placeholder="error || ''" />
    <span v-if="error && code" class="field-help" style="color: var(--error-text)">{{ error }}</span>
    <span class="field-help">{{ t('compute.addWorker.secret') }}</span>
    <template #footer>
      <div style="display: flex; justify-content: flex-end; gap: 8px">
        <NButton @click="addWorkerOpen = false">{{ t('common.done') }}</NButton>
        <NButton type="primary" :disabled="!code" @click="copy"><template #icon><AppIcon name="copy" /></template>{{ t('compute.addWorker.copy') }}</NButton>
      </div>
    </template>
  </NModal>
</template>

<style scoped>
.add-worker-steps { padding-left: 18px; margin-bottom: 14px; }
.field-label { display: block; font-size: 13px; margin-bottom: 6px; }
</style>
