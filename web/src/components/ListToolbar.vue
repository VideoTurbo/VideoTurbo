<script setup lang="ts">
// 列表上方的工具栏：状态视图 + 搜索 + 媒体库筛选。字幕生成和影片修复共用。
import { NInput, NSelect } from 'naive-ui'
import { computed } from 'vue'
import type { ListView } from '@/api/types'
import { viewNames } from '@/lib/status'
import { status } from '@/stores/status'
import AppIcon from './AppIcon.vue'
import { t } from '@/i18n'

defineProps<{ counts: Partial<Record<ListView, number>>; searchPlaceholder: string }>()
const view = defineModel<ListView>('view', { required: true })
const search = defineModel<string>('search', { required: true })
const library = defineModel<string>('library', { required: true })

const libraries = computed(() => [
  { label: t('list.allLibraries'), value: '' },
  ...(status.value?.media_libraries || []).map((item) => ({ label: item.name, value: item.id })),
])
</script>

<template>
  <div class="toolbar">
    <div class="segmented" role="tablist">
      <button v-for="item in viewNames" :key="item.key" role="tab" :class="{ active: view === item.key }" :aria-selected="view === item.key" @click="view = item.key">
        {{ item.label }}<span class="seg-count">{{ counts[item.key] ?? 0 }}</span>
      </button>
    </div>
    <span class="spacer" />
    <NInput v-model:value="search" class="search" clearable :placeholder="searchPlaceholder">
      <template #prefix><AppIcon name="search" /></template>
    </NInput>
    <NSelect v-model:value="library" class="library-select" filterable :options="libraries" :aria-label="t('list.library')" />
  </div>
</template>
