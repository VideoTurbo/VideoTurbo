<script setup lang="ts">
// 列表底部：条数摘要 + 分页。
import { NPagination } from 'naive-ui'
import { computed } from 'vue'
import { t } from '@/i18n'

const props = defineProps<{ total: number; pages: number; shown: number; pageSize: number }>()
const page = defineModel<number>('page', { required: true })

const summary = computed(() => {
  if (!props.total) return t('list.totalNone')
  const start = (page.value - 1) * props.pageSize + 1
  return t('list.totalRange', { total: props.total, start, end: start + props.shown - 1 })
})
</script>

<template>
  <div class="table-footer">
    <span>{{ summary }}</span>
    <NPagination v-if="pages > 1" v-model:page="page" :page-count="pages" :page-slot="7" show-quick-jumper size="small" />
  </div>
</template>
