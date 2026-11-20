<script setup lang="ts">
// 提醒抽屉：最近 100 条提醒。
import { NButton, NDrawer, NDrawerContent } from 'naive-ui'
import { ref, watch } from 'vue'
import { api, errorMessage, type Notice } from '@/api'
import { toast } from '@/lib/feedback'
import { fullDate } from '@/lib/format'
import { setNoticeSummary } from '@/stores/notices'
import { t } from '@/i18n'

const show = defineModel<boolean>({ required: true })
const items = ref<Notice[] | null>(null)
const error = ref('')
const marking = ref(false)
const levels: Record<string, string> = { info: t('notices.level.info'), success: t('notices.level.success'), warning: t('notices.level.warning'), error: t('notices.level.error') }

watch(show, async (open) => {
  if (!open) return
  items.value = null
  error.value = ''
  try {
    const body = await api.notices()
    items.value = body.items || []
    setNoticeSummary(body)
  } catch (cause) {
    error.value = errorMessage(cause)
  }
})

async function markAllRead(): Promise<void> {
  marking.value = true
  try {
    setNoticeSummary(await api.markNoticesRead())
    const now = new Date().toISOString()
    items.value = (items.value || []).map((item) => ({ ...item, read_at: item.read_at || now }))
    toast.ok(t('notices.markedRead'))
  } catch (cause) {
    toast.error(t('notices.markFailed'), cause)
  } finally {
    marking.value = false
  }
}
</script>

<template>
  <NDrawer v-model:show="show" :width="440" style="max-width: 100vw">
    <NDrawerContent closable>
      <template #header>
        <div style="display: flex; align-items: center; gap: 12px">{{ t('notices.title') }}<NButton size="small" :loading="marking" @click="markAllRead">{{ t('notices.markAll') }}</NButton></div>
      </template>
      <div v-if="error" class="warning bad">{{ t('notices.loadFailed', { error }) }}</div>
      <div v-else-if="!items" class="muted">{{ t('common.loading') }}</div>
      <div v-else-if="!items.length" class="empty">{{ t('notices.empty') }}</div>
      <div v-for="notice in items || []" :key="notice.id" class="notice-item" :class="[`lv-${notice.level}`, { unread: !notice.read_at }]">
        <div class="notice-head"><b>{{ notice.title }}</b><span v-if="!notice.read_at" class="notice-read-state">{{ t('notices.unread') }}</span></div>
        <div class="notice-msg">{{ notice.message }}</div>
        <div class="notice-meta">{{ levels[notice.level] || notice.level }} · {{ fullDate(notice.created_at) }}</div>
      </div>
    </NDrawerContent>
  </NDrawer>
</template>
