<script setup lang="ts">
// 可排序的表头单元格。
import { computed } from 'vue'
import type { Sort, SortColumn } from '@/lib/jobs'

const props = defineProps<{ column: SortColumn; sort: Sort }>()
defineEmits<{ sort: [column: SortColumn] }>()

const direction = computed(() => {
  const [column, order] = props.sort.split('_')
  return column === props.column ? (order === 'asc' ? 'ascending' : 'descending') : undefined
})
</script>

<template>
  <th class="sortable" :aria-sort="direction">
    <button type="button" @click="$emit('sort', column)"><slot /></button>
  </th>
</template>
