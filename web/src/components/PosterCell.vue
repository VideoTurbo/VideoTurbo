<script setup lang="ts">
// 海报缩略图；图片加载失败时显示“无海报”。
import { ref, watch } from 'vue'
import { t } from '@/i18n'

const props = defineProps<{ url: string | null | undefined }>()
const failed = ref(false)
watch(() => props.url, () => { failed.value = false })
</script>

<template>
  <td class="poster-col">
    <img v-if="url && !failed" class="poster" loading="lazy" decoding="async" :src="url" alt="" @error="failed = true" />
    <span v-else class="poster-empty">{{ t('list.noPoster') }}</span>
  </td>
</template>
