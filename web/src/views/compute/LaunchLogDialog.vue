<script setup lang="ts">
// 创建任务的日志；任务进行中每 3 秒刷新，用户停在底部时自动滚动。
// 弹窗只显示最近一部分，“下载完整日志”取主程序保存的全部内容。
import { NButton, NModal } from 'naive-ui'
import { nextTick, onUnmounted, ref, watch } from 'vue'
import { api, errorMessage, type Launch } from '@/api'
import AppIcon from '@/components/AppIcon.vue'
import { launchActive, launchLabels } from '@/lib/cloud'
import { launchLogId } from './dialogs'
import { t } from '@/i18n'

const launch = ref<Launch | null>(null)
const error = ref('')
const pre = ref<HTMLPreElement | null>(null)
let timer: ReturnType<typeof setInterval> | undefined

async function refresh(): Promise<void> {
  const id = launchLogId.value
  if (!id) return
  const atBottom = !pre.value || pre.value.scrollHeight - pre.value.scrollTop - pre.value.clientHeight < 40
  try {
    const data = await api.launch(id)
    if (launchLogId.value !== id) return
    launch.value = data
    error.value = ''
    if (!launchActive(data.state)) clearInterval(timer)
    await nextTick()
    if (atBottom && pre.value) pre.value.scrollTop = pre.value.scrollHeight
  } catch (cause) {
    error.value = errorMessage(cause)
  }
}

watch(launchLogId, (id) => {
  clearInterval(timer)
  launch.value = null
  if (!id) return
  void refresh()
  timer = setInterval(refresh, 3000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <NModal :show="launchLogId !== null" preset="card" class="modal-card wide" :title="launch ? t('compute.log.titleGpu', { gpu: launch.gpu_label }) : t('compute.log.title')" @update:show="launchLogId = null">
    <template #header-extra>
      <NButton v-if="launchLogId" size="small" tag="a" :href="api.launchLogUrl(launchLogId)" download>
        <template #icon><AppIcon name="download" /></template>{{ t('compute.log.download') }}
      </NButton>
    </template>
    <div v-if="launch" class="lead">
      {{ launchLabels[launch.state] || launch.state }} · {{ launch.message || '' }}{{ launch.instance_id ? t('compute.log.host', { id: launch.instance_id }) : '' }}{{ launch.machine ? ` · ${launch.machine}` : '' }}
    </div>
    <pre ref="pre" class="launch-log">{{ error ? t('compute.log.loadFailed', { error }) : launch ? launch.log || t('compute.log.empty') : t('common.loading') }}</pre>
  </NModal>
</template>
