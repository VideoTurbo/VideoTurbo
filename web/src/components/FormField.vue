<script setup lang="ts">
// 表单的一项：标签（必填加红色 *，选填加“（选填）”，可带 “?” 说明）+ 控件 + 帮助文字。
import HelpTip from './HelpTip.vue'
import { t } from '@/i18n'
defineProps<{ label: string; tip?: string; help?: string; required?: boolean; optional?: boolean }>()
</script>

<template>
  <div class="field">
    <span class="field-label">
      <span>{{ label }}<span v-if="required" class="required-mark" :title="t('common.required')" :aria-label="t('common.required')">*</span><span v-else-if="optional" class="optional-mark">{{ t('common.optionalSuffix') }}</span></span>
      <HelpTip v-if="tip" :text="tip" />
    </span>
    <slot />
    <span v-if="help" class="field-help">{{ help }}</span>
  </div>
</template>
