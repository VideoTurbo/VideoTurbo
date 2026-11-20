<script setup lang="ts">
// 带说明文字的开关行。
import { NSwitch } from 'naive-ui'
import HelpTip from './HelpTip.vue'
defineProps<{ label: string; tip?: string; help?: string; disabled?: boolean }>()
const value = defineModel<boolean>({ required: true })
</script>

<template>
  <label class="switch-row" :class="{ disabled }">
    <span>
      <span class="field-label">{{ label }}<HelpTip v-if="tip" :text="tip" /></span>
      <span v-if="help" class="field-help">{{ help }}</span>
    </span>
    <NSwitch v-model:value="value" :disabled="disabled" />
  </label>
</template>
