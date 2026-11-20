<script setup lang="ts">
// Worker 上报的影片修复进度：下载 / 回传 / 修复 / 排队。
import type { TransferProgress } from '@/api/types'
import { processingText, stageName, transferText } from '@/lib/restore'
import ProgressBar from './ProgressBar.vue'

defineProps<{ progress: TransferProgress }>()
const isTransfer = (p: TransferProgress) => p.total_bytes != null && (p.stage === 'downloading' || p.stage === 'uploading')
</script>

<template>
  <template v-if="isTransfer(progress)">
    <div class="info-head">
      <b>{{ stageName(progress.stage) }}{{ progress.detail && progress.detail !== stageName(progress.stage) ? ` · ${progress.detail}` : '' }}</b>
    </div>
    <ProgressBar :percent="progress.percent" />
    <div class="dc-sub">{{ transferText(progress) }}</div>
  </template>
  <template v-else-if="progress.stage === 'processing'">
    <div class="info-head">
      <b>{{ progress.detail || stageName(progress.stage) }}</b>
      <span class="num">{{ Number(progress.percent || 0).toFixed(1) }}%</span>
    </div>
    <ProgressBar :percent="progress.percent" />
    <div class="dc-sub">{{ processingText(progress) }}</div>
  </template>
  <template v-else>
    <div class="info-head"><b>{{ stageName(progress.stage) }}</b></div>
    <div v-if="progress.detail" class="dc-sub">{{ progress.detail }}</div>
  </template>
</template>
