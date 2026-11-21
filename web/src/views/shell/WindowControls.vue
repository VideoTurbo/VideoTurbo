<script setup lang="ts">
// 桌面版 Windows：系统标题栏去掉了，顶栏右侧画最小化、最大化/还原、关闭三个按钮，样式仿照系统。
// 关闭只是隐藏窗口，主程序在托盘里继续运行（外壳处理）。
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { t } from '@/i18n'
import { shellInvoke } from '@/lib/shell'

const maximized = ref(false)

async function refresh(): Promise<void> {
  maximized.value = (await shellInvoke<boolean>('plugin:window|is_maximized')) ?? false
}

onMounted(() => {
  void refresh()
  window.addEventListener('resize', refresh)
})
onBeforeUnmount(() => window.removeEventListener('resize', refresh))
</script>

<template>
  <div class="window-controls">
    <button type="button" :aria-label="t('app.window.minimize')" :title="t('app.window.minimize')" @click="shellInvoke('plugin:window|minimize')">
      <svg viewBox="0 0 10 10" aria-hidden="true"><path d="M0 5.5h10" /></svg>
    </button>
    <button type="button" :aria-label="maximized ? t('app.window.restore') : t('app.window.maximize')" :title="maximized ? t('app.window.restore') : t('app.window.maximize')" @click="shellInvoke('plugin:window|toggle_maximize')">
      <svg v-if="maximized" viewBox="0 0 10 10" aria-hidden="true"><path d="M2.5 2.5V.5h7v7h-2M.5 2.5h7v7h-7z" /></svg>
      <svg v-else viewBox="0 0 10 10" aria-hidden="true"><path d="M.5.5h9v9h-9z" /></svg>
    </button>
    <button type="button" class="close" :aria-label="t('app.window.close')" :title="t('app.window.close')" @click="shellInvoke('plugin:window|close')">
      <svg viewBox="0 0 10 10" aria-hidden="true"><path d="M.5.5l9 9M9.5.5l-9 9" /></svg>
    </button>
  </div>
</template>
