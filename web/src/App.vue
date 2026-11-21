<script setup lang="ts">
// 外壳：顶栏、主导航和当前页面。页面用 KeepAlive 保留筛选与勾选状态。
import { NButton, NConfigProvider, NDropdown, NTooltip, type DropdownOption } from 'naive-ui'
import { computed, h, ref, type Component } from 'vue'
import { api } from '@/api'
import AppIcon from '@/components/AppIcon.vue'
import { naiveDateLocale, naiveLocale, t } from '@/i18n'
import { toast } from '@/lib/feedback'
import { titlebar } from '@/lib/shell'
import { switchTab, tab, type Tab } from '@/stores/navigation'
import { noticesOpen, unread } from '@/stores/notices'
import { refreshStatus, status } from '@/stores/status'
import type { IconName } from '@/components/icons'
import { naiveOverrides, naiveTheme, setThemeMode, themeMode, type ThemeMode } from '@/theme'
import ComputeView from '@/views/compute/ComputeView.vue'
import LlmView from '@/views/llm/LlmView.vue'
import RestoreView from '@/views/restore/RestoreView.vue'
import SettingsView from '@/views/settings/SettingsView.vue'
import NoticesDrawer from '@/views/shell/NoticesDrawer.vue'
import WindowControls from '@/views/shell/WindowControls.vue'
import ScanDialog from '@/views/shell/ScanDialog.vue'
import SubtitleView from '@/views/subtitle/SubtitleView.vue'

const views: Record<Tab, Component> = { subtitle: SubtitleView, restore: RestoreView, compute: ComputeView, translate: LlmView, settings: SettingsView }
const tabs: { id: Tab; label: string }[] = [
  { id: 'subtitle', label: t('app.tab.subtitle') }, { id: 'restore', label: t('app.tab.restore') }, { id: 'compute', label: t('app.tab.compute') }, { id: 'translate', label: t('app.tab.translate') },
]

const scanOpen = ref(false)

// ---- 主题：跟随系统 / 日间 / 夜间 ----
const themeIcons: Record<ThemeMode, IconName> = { system: 'monitor', light: 'sun', dark: 'moon' }
const themeLabel = (mode: ThemeMode) => t(`app.theme.${mode}` as const)
const themeOptions = computed<DropdownOption[]>(() => (['system', 'light', 'dark'] as const).map((mode) => ({
  key: mode,
  icon: () => h(AppIcon, { name: themeIcons[mode] }),
  label: () => h('span', { class: 'theme-option' }, [themeLabel(mode), themeMode.value === mode ? h(AppIcon, { name: 'check' }) : null]),
})))
const themeTitle = computed(() => t('app.theme.current', { mode: themeLabel(themeMode.value) }))

// 桌面版：顶栏就是窗口标题栏，空白处可拖动、双击最大化。macOS 让出左上角的系统窗口按钮，Windows 在右侧画窗口按钮。
const dragRegion = titlebar ? 'deep' : undefined

// ---- 顶栏只有一个扫描按钮：平时“扫描影片”，扫描中变成“停止扫描” ----
const scanning = computed(() => status.value?.scan_status === 'running')
const scanBlocked = computed(() => {
  const data = status.value
  if (!data) return t('app.scan.connecting')
  if (data.pipeline_paused) return t('app.scan.paused')
  if (data.manual_scan_enabled === false) return t('app.scan.preview')
  return data.media_libraries.some((library) => library.enabled) ? '' : t('app.scan.noLibrary')
})
const scanLive = computed(() => {
  const scan = status.value?.last_scan
  return scanning.value ? t('app.scan.live', { library: scan?.current_library || '', count: scan?.scanned || 0, path: scan?.current_path || t('app.scan.walking') }) : ''
})

async function stopScan(): Promise<void> {
  try {
    await api.cancelScan()
    toast.ok(t('app.scan.stopping'))
  } catch (error) {
    toast.error(t('common.stopFailed'), error)
  }
  await refreshStatus()
}
</script>

<template>
  <NConfigProvider :theme="naiveTheme" :theme-overrides="naiveOverrides" :locale="naiveLocale" :date-locale="naiveDateLocale">
    <header class="topbar" :data-tauri-drag-region="dragRegion">
      <div class="topbar-inner">
        <a class="brand" href="/" :aria-label="t('app.brandHome')">
          <!-- 图标由主控提供，不经过打包，所以用绑定写法。换图标时改 v，浏览器缓存一天，不改会继续显示旧图标。 -->
          <img :src="'/favicon.svg?v=2'" alt="" width="22" height="22" />
          <h1>VideoTurbo</h1>
          <span v-if="status?.version" class="brand-version">v{{ status.version }}</span>
        </a>
        <nav class="tabs" role="tablist" :aria-label="t('app.mainNav')">
          <button v-for="item in tabs" :key="item.id" class="tab" :class="{ active: tab === item.id }" role="tab" :aria-selected="tab === item.id" @click="switchTab(item.id)">
            {{ item.label }}
          </button>
        </nav>
        <div class="top-actions">
          <NButton v-if="scanning" class="scan-button" type="error" secondary @click="stopScan">
            <template #icon><AppIcon name="x" /></template>{{ t('app.scan.stop') }}
          </NButton>
          <NButton v-else class="scan-button" type="primary" :disabled="!!scanBlocked" :title="scanBlocked" @click="scanOpen = true">
            <template #icon><AppIcon name="play" /></template>{{ t('app.scan.start') }}
          </NButton>
          <NTooltip>
            <template #trigger>
              <NButton quaternary :aria-label="t('app.notices')" @click="noticesOpen = true">
                <template #icon><AppIcon name="bell" /></template>
                <span v-if="unread" class="notice-badge">{{ unread > 99 ? '99+' : unread }}</span>
              </NButton>
            </template>
            {{ t('app.noticesTip') }}
          </NTooltip>
          <NDropdown trigger="click" placement="bottom-end" :options="themeOptions" @select="(mode: ThemeMode) => setThemeMode(mode)">
            <NButton quaternary :aria-label="themeTitle" :title="themeTitle">
              <template #icon><AppIcon :name="themeIcons[themeMode]" /></template>
            </NButton>
          </NDropdown>
          <NButton quaternary :aria-label="t('app.settings')" :title="t('app.settings')" :type="tab === 'settings' ? 'primary' : 'default'" @click="switchTab('settings')">
            <template #icon><AppIcon name="gear" /></template>
          </NButton>
        </div>
      </div>
      <WindowControls v-if="titlebar === 'custom'" />
    </header>

    <main class="shell">
      <div v-if="scanLive" class="live-line"><span>{{ scanLive }}</span></div>
      <KeepAlive><component :is="views[tab]" /></KeepAlive>
    </main>

    <ScanDialog v-model="scanOpen" />
    <NoticesDrawer v-model="noticesOpen" />
  </NConfigProvider>
</template>
