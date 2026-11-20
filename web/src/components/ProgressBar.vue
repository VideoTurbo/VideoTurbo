<script setup lang="ts">
import { clampPercent } from '@/lib/format'
defineProps<{ percent: number | null | undefined }>()
</script>

<template>
  <div class="mini-progress"><span :style="{ width: `${clampPercent(percent)}%` }" /></div>
</template>
