<script setup lang="ts">
// 勾选后浮在底部的批量操作条。
import { NButton } from 'naive-ui'
import AppIcon from './AppIcon.vue'
import { t } from '@/i18n'

defineProps<{ count: number; label?: string }>()
defineEmits<{ clear: [] }>()
</script>

<template>
  <div v-if="count > 0" class="dock">
    <span class="dock-count">{{ label || t('common.selectedCount', { count }) }}</span>
    <slot />
    <NButton size="small" quaternary :aria-label="t('common.clearSelection')" :title="t('common.clearSelection')" @click="$emit('clear')">
      <template #icon><AppIcon name="x" /></template>
    </NButton>
  </div>
</template>
