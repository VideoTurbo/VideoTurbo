<script setup lang="ts">
import { icons, type IconName } from './icons'

defineProps<{ name: IconName }>()
const solid = { fill: 'currentColor', stroke: 'none' }
</script>

<template>
  <svg class="i" viewBox="0 0 24 24" aria-hidden="true">
    <path :d="icons[name].d" v-bind="'solid' in icons[name] ? solid : undefined" />
  </svg>
</template>
