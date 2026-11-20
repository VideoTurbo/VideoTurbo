<script setup lang="ts">
// 手动扫描：选择本次要扫描的媒体库，提交后轮询到扫描结束。
import { NButton, NCheckbox, NModal } from 'naive-ui'
import { computed, ref, watch } from 'vue'
import { api } from '@/api'
import { toast } from '@/lib/feedback'
import { refreshStatus, status } from '@/stores/status'
import { t } from '@/i18n'

const show = defineModel<boolean>({ required: true })
const libraries = computed(() => status.value?.media_libraries || [])
const picked = ref(new Set<string>())
const submitting = ref(false)

// 每次打开默认选中所有已启用的库。
watch(show, (open) => {
  if (open) picked.value = new Set(libraries.value.filter((library) => library.enabled).map((library) => library.id))
})

function toggle(id: string, checked: boolean): void {
  if (checked) picked.value.add(id)
  else picked.value.delete(id)
}

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

/** 每秒看一次状态，直到出现一次新的、已结束的扫描；最多等两分钟。 */
async function waitForScan(previousStart: string | undefined): Promise<void> {
  for (let attempt = 0; attempt < 120; attempt++) {
    await wait(1000)
    const scan = (await refreshStatus())?.last_scan
    if (scan?.started_at && scan.started_at !== previousStart && scan.status !== 'running') {
      return toast.ok(scan.status === 'cancelled'
        ? t('scan.stopped', { scanned: scan.scanned || 0 })
        : t('scan.done', { scanned: scan.scanned || 0, pending: scan.pending || 0 }))
    }
  }
  toast.info(t('scan.running'))
}

async function start(): Promise<void> {
  if (!picked.value.size) return toast.fail(t('scan.selectLibrary'))
  const previousStart = status.value?.last_scan?.started_at
  submitting.value = true
  try {
    await api.startScan([...picked.value])
    show.value = false
    toast.ok(t('scan.submitted'))
    void waitForScan(previousStart)
  } catch (error) {
    toast.error(t('jobs.action.submitFailed'), error)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <NModal v-model:show="show" preset="card" class="modal-card" :title="t('scan.title')">
    <p class="lead">{{ t('scan.help') }}</p>
    <div class="option-list">
      <label v-for="library in libraries" :key="library.id" class="option" :class="{ disabled: !library.enabled, checked: picked.has(library.id) }">
        <NCheckbox :checked="picked.has(library.id)" :disabled="!library.enabled" @update:checked="(checked: boolean) => toggle(library.id, checked)" />
        <span>{{ library.name }}<span v-if="!library.enabled" class="option-meta">{{ t('scan.disabled') }}</span></span>
      </label>
    </div>
    <template #footer>
      <div class="modal-actions">
        <NButton @click="show = false">{{ t('common.cancel') }}</NButton>
        <NButton type="primary" :loading="submitting" @click="start">{{ t('scan.start') }}</NButton>
      </div>
    </template>
  </NModal>
</template>
