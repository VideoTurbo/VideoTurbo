<script setup lang="ts">
// 字段名后面的 “?” 说明。
import { NTooltip } from 'naive-ui'
defineProps<{ text: string }>()
</script>

<template>
  <NTooltip trigger="hover">
    <template #trigger><span class="tip" tabindex="0">?</span></template>
    <div class="tip-text">{{ text }}</div>
  </NTooltip>
</template>
