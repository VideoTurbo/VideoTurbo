<script setup lang="ts">
// 媒体库列表。每项操作立即保存，不计入“未保存的更改”。网盘插件的账号等在“插件”里的插件设置中填写。
import { NButton, NSwitch } from 'naive-ui'
import { computed, onActivated, onMounted, ref } from 'vue'
import { api, errorMessage, type LanguageInfo, type MediaLibrary, type MediaSourceOption } from '@/api'
import AppIcon from '@/components/AppIcon.vue'
import MoreMenu from '@/components/MoreMenu.vue'
import type { MenuItem } from '@/components/menu'
import { confirm, toast } from '@/lib/feedback'
import { relativeTime } from '@/lib/format'
import { refreshStatus, status } from '@/stores/status'
import LibraryDialog from './LibraryDialog.vue'
import { t } from '@/i18n'

defineProps<{ languages: LanguageInfo[] }>()

const libraries = ref<MediaLibrary[]>([])
/** 已安装的网盘插件，添加媒体库时可选。 */
const sources = ref<MediaSourceOption[]>([])
const loaded = ref(false)
const loadError = ref('')

async function load(): Promise<void> {
  try {
    const [list, sourceList] = await Promise.all([api.libraries(), api.librarySources()])
    libraries.value = list
    sources.value = sourceList
    loadError.value = ''
  } catch (error) {
    loadError.value = errorMessage(error)
  } finally {
    loaded.value = true
  }
}
onMounted(load)
onActivated(load)

async function changed(): Promise<void> {
  await Promise.all([load(), refreshStatus()])
}

// ---- 添加和编辑 ----
const dialogOpen = ref(false)
const editing = ref<MediaLibrary | null>(null)

function openDialog(library: MediaLibrary | null): void {
  editing.value = library
  dialogOpen.value = true
}

async function toggle(library: MediaLibrary, enabled: boolean): Promise<void> {
  try {
    await api.updateLibrary(library.id, { name: library.name, enabled, source_language: library.source_language, target_language: library.target_language })
    library.enabled = enabled
    await refreshStatus()
  } catch (error) {
    toast.error(t('compute.providers.saveFailedShort'), error)
  }
}

async function remove(library: MediaLibrary): Promise<void> {
  if (!await confirm(
    t('settings.libraries.deleteConfirm', { name: library.name, count: library.media_count }),
    { title: t('settings.libraries.deleteTitle'), confirmLabel: t('common.delete'), danger: true },
  )) return
  try {
    await api.deleteLibrary(library.id)
    dialogOpen.value = false
    toast.ok(t('settings.libraries.deleted', { name: library.name }))
    await changed()
  } catch (error) {
    toast.error(t('common.deleteFailed'), error)
  }
}

/** 手动扫描关闭或正在扫描时不显示“立即扫描”。 */
const canScan = computed(() => status.value?.manual_scan_enabled !== false && status.value?.scan_status !== 'running')

async function scan(library: MediaLibrary): Promise<void> {
  try {
    await api.startScan([library.id])
    toast.ok(t('settings.libraries.scanStarted', { name: library.name }))
    await refreshStatus()
  } catch (error) {
    toast.error(t('settings.libraries.scanFailed'), error)
  }
}

function menu(library: MediaLibrary): MenuItem[] {
  return [
    { label: t('common.edit'), icon: 'edit', run: () => openDialog(library) },
    ...(library.enabled && !library.problem && canScan.value
      ? [{ label: t('settings.libraries.scanNow'), icon: 'refresh', run: () => scan(library) } as const]
      : []),
    '-',
    { label: t('common.delete'), icon: 'trash', danger: true, run: () => remove(library) },
  ]
}

/** 路径放在最后：太长时被省略的是路径，不是影片数和扫描时间。 */
function meta(library: MediaLibrary): string {
  const scanned = library.scanned_at ? t('settings.libraries.scanned', { count: library.media_count, time: relativeTime(library.scanned_at) }) : t('settings.libraries.neverScanned')
  return [library.source_label, scanned, library.path].join(' · ')
}
</script>

<template>
  <div class="settings-section">
    <div class="section-head" style="margin: 0; align-items: center">
      <h3>{{ t('settings.libraries.title') }}</h3><span class="spacer" />
      <NButton size="small" type="primary" @click="openDialog(null)"><template #icon><AppIcon name="plus" /></template>{{ t('settings.libraries.add') }}</NButton>
    </div>
    <p class="lead">{{ t('settings.libraries.lead') }}</p>
    <div v-if="loadError" class="warning bad">{{ t('settings.libraries.loadFailed', { error: loadError }) }}</div>
    <div v-else-if="loaded && !libraries.length" class="library-empty">
      <AppIcon name="folder" />
      <b>{{ t('settings.libraries.addFirst') }}</b>
      <span>{{ t('settings.libraries.addFirstHelp') }}</span>
      <NButton size="small" @click="openDialog(null)">{{ t('settings.libraries.add') }}</NButton>
    </div>
    <div v-else class="library-cards">
      <div
        v-for="library in libraries" :key="library.id" class="library-card" :class="{ off: !library.enabled }"
        role="button" tabindex="0" :aria-label="t('settings.libraries.editName', { name: library.name })"
        @click="openDialog(library)" @keydown.enter.self="openDialog(library)"
      >
        <span class="plugin-icon">
          <img v-if="library.source_icon" :src="library.source_icon" alt="">
          <AppIcon v-else :name="library.source === 'local' ? 'folder' : 'cloud'" />
        </span>
        <div class="library-card-text">
          <div class="library-card-title">
            <b>{{ library.name }}</b>
            <span class="tag">{{ library.source_language_label }} → {{ library.target_language_label }}</span>
            <span v-if="!library.enabled" class="tag">{{ t('settings.libraries.disabled') }}</span>
          </div>
          <span class="library-card-meta" :title="library.path">{{ meta(library) }}</span>
          <span v-if="library.problem" class="cloud-result bad">{{ library.problem }}</span>
        </div>
        <div class="library-card-actions" @click.stop @keydown.stop>
          <NSwitch :value="library.enabled" :aria-label="t('settings.libraries.enableName', { name: library.name })" @update:value="(value: boolean) => toggle(library, value)" />
          <MoreMenu :items="menu(library)" />
        </div>
      </div>
    </div>
  </div>

  <LibraryDialog
    v-model="dialogOpen" :library="editing" :sources="sources" :languages="languages"
    @saved="changed" @sources-changed="changed" @remove="remove"
  />
</template>
