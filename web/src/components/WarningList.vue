<script setup lang="ts">
// 页面顶部的提醒。带跳转位置的提醒可以点击，直接打开对应的页面或设置分类。
import type { Warning } from '@/api'
import AppIcon from '@/components/AppIcon.vue'
import { t } from '@/i18n'
import { openTarget } from '@/stores/navigation'

defineProps<{ items: Warning[] }>()
</script>

<template>
  <template v-for="item in items" :key="item.message">
    <button v-if="item.target" type="button" class="warning warning-link" :class="{ bad: item.bad }" :title="t('common.openSettings')" @click="openTarget(item.target)">
      <span class="warning-text">{{ item.message }}</span>
      <AppIcon name="chevron" class="warning-go" />
    </button>
    <div v-else class="warning" :class="{ bad: item.bad }">{{ item.message }}</div>
  </template>
</template>
