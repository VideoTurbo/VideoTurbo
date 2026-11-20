<script setup lang="ts">
// 卡片 / 列表视图切换。
import type { ViewMode } from '@/lib/viewMode'
import AppIcon from './AppIcon.vue'
import { t } from '@/i18n'

const mode = defineModel<ViewMode>({ required: true })
const options = [
  { value: 'card', label: t('list.cardView'), icon: 'grid' },
  { value: 'list', label: t('list.listView'), icon: 'list' },
] as const
</script>

<template>
  <div class="segmented view-toggle" role="group" :aria-label="t('list.view')">
    <button v-for="item in options" :key="item.value" type="button" :class="{ active: mode === item.value }"
      :aria-pressed="mode === item.value" :aria-label="item.label" :title="item.label" @click="mode = item.value">
      <AppIcon :name="item.icon" />
    </button>
  </div>
</template>
