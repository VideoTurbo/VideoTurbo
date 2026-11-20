<script setup lang="ts">
// “更多”菜单：鼠标移上去即展开。菜单项在打开时才计算，列表刷新不会打断它。
import { NButton, NDropdown, type DropdownOption } from 'naive-ui'
import { computed, h } from 'vue'
import AppIcon from './AppIcon.vue'
import type { MenuItem } from './menu'
import { t } from '@/i18n'

const props = defineProps<{ items: MenuItem[]; text?: string }>()

const actions = computed(() => new Map(props.items.flatMap((item, index) =>
  typeof item === 'object' && 'run' in item ? [[index, item.run] as const] : [])))

const options = computed<DropdownOption[]>(() => props.items.map((item, index) => {
  if (item === '-') return { type: 'divider', key: `divider-${index}` }
  if ('heading' in item) return { type: 'render', key: `heading-${index}`, render: () => h('div', { class: 'menu-heading' }, item.heading) }
  return {
    key: index,
    label: item.label,
    icon: () => h(AppIcon, { name: item.icon }),
    props: item.danger ? { style: 'color:var(--red)' } : undefined,
  }
}))
</script>

<template>
  <NDropdown v-if="items.length" trigger="hover" placement="bottom-end" :options="options" @select="(key: number) => actions.get(key)?.()">
    <NButton v-if="text" size="small">{{ text }}</NButton>
    <NButton v-else size="small" quaternary :aria-label="t('common.moreActions')">
      <template #icon><AppIcon name="more" /></template>
    </NButton>
  </NDropdown>
</template>

<style>
.menu-heading{padding:6px 12px 3px;font-size:11px;color:var(--text-3)}
</style>
