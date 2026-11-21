<script setup lang="ts">
// 添加或编辑媒体库。添加分三步：选来源 → 选文件夹 → 名称和语言；编辑只改名称、语言和启用，来源和文件夹不能改。
// 来源是本地文件夹、已安装的网盘插件或网络存储（WebDAV、Samba，在“网络存储”里添加）；
// 网盘插件的账号等在插件设置里填写，还没设置的点一下就打开设置弹窗。
import { NButton, NInput, NModal, NSelect } from 'naive-ui'
import { computed, reactive, ref, watch } from 'vue'
import { api, errorMessage, type FolderItem, type FolderPage, type LanguageInfo, type MediaLibrary, type MediaSourceOption } from '@/api'
import AppIcon from '@/components/AppIcon.vue'
import FormField from '@/components/FormField.vue'
import SwitchRow from '@/components/SwitchRow.vue'
import { toast } from '@/lib/feedback'
import PluginSettingsDialog from './PluginSettingsDialog.vue'
import { t } from '@/i18n'
import { openSettings } from '@/stores/navigation'
import { isDesktop } from '@/stores/status'

const props = defineProps<{
  /** 正在编辑的媒体库；null 表示添加。 */
  library: MediaLibrary | null
  /** 已安装的网盘插件。 */
  sources: MediaSourceOption[]
  languages: LanguageInfo[]
}>()
const show = defineModel<boolean>({ required: true })
/** sources-changed：插件设置保存后，父组件重新读取可选的网盘插件。 */
const emit = defineEmits<{ saved: []; 'sources-changed': []; remove: [library: MediaLibrary] }>()

const LOCAL = 'local'
type Step = 'source' | 'folder' | 'details'
const step = ref<Step>('source')
const form = reactive({ source: LOCAL, path: '', name: '', enabled: true, source_language: 'ja', target_language: 'zh-CN' })
const busy = ref(false)
const error = ref('')

watch(show, (open) => {
  if (!open) return
  const item = props.library
  Object.assign(form, item
    ? { source: item.source, path: item.path, name: item.name, enabled: item.enabled, source_language: item.source_language, target_language: item.target_language }
    // 新建媒体库默认日语 → 简体中文。
    : { source: LOCAL, path: '', name: '', enabled: true, source_language: 'ja', target_language: 'zh-CN' })
  step.value = item ? 'details' : 'source'
  error.value = ''
  page.value = null
})

// ---- 第 1 步：来源 ----
/** 来源图标：编辑时用媒体库的，添加时用所选来源的。 */
const sourceIcon = computed(() => props.library?.source_icon || props.sources.find((item) => item.id === form.source)?.icon || '')
const sourceLabel = computed(() => form.source === LOCAL
  ? t('settings.library.local')
  : props.sources.find((item) => item.id === form.source)?.label || props.library?.source_label || form.source)

function pickSource(source: string): void {
  form.source = source
  step.value = 'folder'
  void browse(null)
}

/** 网盘插件还没设置好时先打开设置弹窗，保存后再选它。 */
const settingsOpen = ref(false)
const settingsPlugin = ref<string | null>(null)

function addStorage(): void {
  show.value = false
  openSettings('storage')
}

function pickPlugin(source: MediaSourceOption): void {
  if (!source.problem) return pickSource(source.id)
  settingsPlugin.value = source.id
  settingsOpen.value = true
}

// ---- 第 2 步：文件夹 ----
const page = ref<FolderPage | null>(null)
const loadingFolders = ref(false)

async function browse(path: string | null): Promise<void> {
  loadingFolders.value = true
  error.value = ''
  try {
    page.value = await api.browseFolders(form.source, path)
  } catch (failure) {
    error.value = errorMessage(failure)
  } finally {
    loadingFolders.value = false
  }
}

/** 面包屑：从根目录（本地的媒体目录，或网盘的 /）开始。 */
const crumbs = computed(() => {
  const path = page.value?.path
  const root = page.value?.root
  if (!path || !root) return []
  const base = root === '/' ? '' : root
  const parts = path.slice(base.length).split('/').filter(Boolean)
  return [
    { name: root === '/' ? t('settings.library.driveRoot') : root, path: root },
    ...parts.map((name, index) => ({ name, path: `${base}/${parts.slice(0, index + 1).join('/')}` })),
  ]
})

/** 当前文件夹已是媒体库、在媒体库里面或里面有媒体库时不能选：否则同一部影片会被两个媒体库扫描。 */
const currentBlocked = computed(() => {
  const current = page.value?.current
  if (!current) return t('settings.library.openFirst')
  if (current.library) return t('settings.library.alreadyLibrary', { library: current.library })
  if (current.inside) return t('settings.library.insideLibrary', { library: current.inside })
  if (current.contains) return t('settings.library.containsLibrary')
  return null
})
/** 里面有媒体库的文件夹多半只是路过（例如刚进入的媒体目录），只禁用“选择”，列表里已标出“已添加”。 */
const currentWarning = computed(() => {
  const current = page.value?.current
  return current && !current.library && !current.inside ? null : currentBlocked.value
})

function folderNote(folder: FolderItem): string {
  if (folder.library) return t('settings.library.added', { library: folder.library })
  if (folder.inside) return t('settings.library.inside', { library: folder.inside })
  return ''
}

function chooseCurrent(): void {
  const path = page.value?.path
  if (!path) return
  form.path = path
  if (!form.name) form.name = path.split('/').filter(Boolean).pop() || ''
  step.value = 'details'
}

// ---- 第 3 步：名称和语言 ----
const sourceOptions = computed(() => props.languages.filter((item) => item.can_be_source).map((item) => ({ value: item.code, label: item.label })))
/** 目标语言不能与源语言相同。 */
const targetOptions = computed(() => props.languages
  .filter((item) => item.can_be_target)
  .map((item) => ({ value: item.code, label: item.label, disabled: item.code === form.source_language })))

function changeSource(source: string): void {
  form.source_language = source
  if (form.target_language === source) {
    form.target_language = targetOptions.value.find((item) => !item.disabled)?.value || form.target_language
  }
}

async function save(): Promise<void> {
  const name = form.name.trim()
  if (!name) return void (error.value = t('settings.library.nameRequired'))
  busy.value = true
  error.value = ''
  const body = { name, enabled: form.enabled, source_language: form.source_language, target_language: form.target_language }
  try {
    if (props.library) await api.updateLibrary(props.library.id, body)
    else await api.createLibrary({ ...body, source: form.source, path: form.path })
    toast.ok(props.library ? t('settings.library.saved') : t('settings.library.created', { name }))
    show.value = false
    emit('saved')
  } catch (failure) {
    error.value = errorMessage(failure)
  } finally {
    busy.value = false
  }
}

const steps: { id: Step; label: string }[] = [
  { id: 'source', label: t('settings.library.step.source') }, { id: 'folder', label: t('settings.library.step.folder') }, { id: 'details', label: t('settings.library.step.details') },
]
const stepIndex = computed(() => steps.findIndex((item) => item.id === step.value))
const title = computed(() => (props.library ? t('settings.library.editTitle', { name: props.library.name }) : t('settings.libraries.add')))
</script>

<template>
  <NModal :show="show" preset="card" class="modal-card" :title="title" @update:show="show = $event">
    <ol v-if="!library" class="wizard-steps" :aria-label="t('settings.library.steps')">
      <li v-for="(item, index) in steps" :key="item.id" :class="{ active: index === stepIndex, done: index < stepIndex }">
        <span class="wizard-step-dot"><AppIcon v-if="index < stepIndex" name="check" /><template v-else>{{ index + 1 }}</template></span>
        {{ item.label }}
      </li>
    </ol>

    <!-- 第 1 步：来源 -->
    <div v-if="step === 'source'" class="source-list">
      <button type="button" class="source-option" @click="pickSource(LOCAL)">
        <span class="plugin-icon"><AppIcon name="folder" /></span>
        <span class="source-option-text"><b>{{ t('settings.library.local') }}</b><span>{{ t(isDesktop ? 'settings.library.localHelp.desktop' : 'settings.library.localHelp') }}</span></span>
        <AppIcon name="chevron" />
      </button>
      <button v-for="source in sources" :key="source.id" type="button" class="source-option" @click="pickPlugin(source)">
        <span class="plugin-icon"><img v-if="source.icon" :src="source.icon" alt=""><AppIcon v-else name="cloud" /></span>
        <span class="source-option-text">
          <b>{{ source.label }}</b>
          <span v-if="source.problem" class="source-option-warn">{{ t('settings.library.notConfigured') }}</span>
          <span v-else>{{ source.description || t('settings.library.driveHelp') }}</span>
        </span>
        <AppIcon :name="source.problem ? 'gear' : 'chevron'" />
      </button>
      <p class="field-help">{{ t('settings.library.storageHint') }} <a href="#" @click.prevent="addStorage">{{ t('settings.nav.storage') }}</a> {{ t('settings.library.storageHintEnd') }}</p>
    </div>

    <!-- 第 2 步：文件夹 -->
    <div v-else-if="step === 'folder'" class="folder-browser">
      <div class="folder-crumbs">
        <span class="tag">{{ sourceLabel }}</span>
        <template v-if="page?.path">
          <template v-for="(crumb, index) in crumbs" :key="crumb.path">
            <span v-if="index" class="muted" aria-hidden="true">/</span>
            <button type="button" class="link-btn" :disabled="crumb.path === page.path" @click="browse(crumb.path)">{{ crumb.name }}</button>
          </template>
        </template>
        <span v-else class="muted">{{ t('settings.library.allFolders') }}</span>
      </div>
      <div class="folder-list" :class="{ loading: loadingFolders }">
        <button v-if="page && page.parent !== null" type="button" class="folder-row" @click="browse(page.parent || null)">
          <AppIcon name="back" /><span class="folder-name">{{ t('settings.library.up') }}</span>
        </button>
        <button v-for="folder in page?.folders || []" :key="folder.path" type="button" class="folder-row" @click="browse(folder.path)">
          <AppIcon name="folder" /><span class="folder-name">{{ folder.name }}</span>
          <span v-if="folderNote(folder)" class="tag">{{ folderNote(folder) }}</span>
          <AppIcon name="chevron" class="folder-open" />
        </button>
        <div v-if="page && !page.folders.length" class="muted key-empty folder-empty">{{ t('settings.library.noSubfolders') }}</div>
      </div>
      <div v-if="page?.current && currentWarning" class="cloud-result bad">{{ currentWarning }}</div>
      <p class="field-help">{{ t('settings.library.pickHelp') }}<template v-if="form.source === LOCAL">{{ t(isDesktop ? 'settings.library.missingHelp.desktop' : 'settings.library.missingHelp') }}</template></p>
    </div>

    <!-- 第 3 步：名称和语言（编辑时只有这一步） -->
    <div v-else>
      <div class="library-summary">
        <span class="plugin-icon"><img v-if="sourceIcon" :src="sourceIcon" alt=""><AppIcon v-else :name="form.source === LOCAL ? 'folder' : 'cloud'" /></span>
        <span class="source-option-text"><b class="mono">{{ form.path }}</b><span>{{ sourceLabel }}<template v-if="library"> {{ t('settings.library.fixedHelp') }}</template></span></span>
        <NButton v-if="!library" size="small" quaternary @click="step = 'folder'">{{ t('settings.library.change') }}</NButton>
      </div>
      <FormField :label="t('compute.providers.name')" required><NInput v-model:value="form.name" maxlength="60" :placeholder="t('settings.library.namePlaceholder')" /></FormField>
      <div class="field-row">
        <FormField :label="t('settings.library.sourceLanguage')">
          <NSelect :value="form.source_language" :options="sourceOptions" @update:value="changeSource" />
        </FormField>
        <FormField :label="t('settings.library.targetLanguage')">
          <NSelect v-model:value="form.target_language" :options="targetOptions" />
        </FormField>
      </div>
      <SwitchRow v-model="form.enabled" :label="t('settings.library.enabled')" :help="t('settings.library.enabledHelp')" />
    </div>

    <div v-if="error" class="cloud-result bad">{{ error }}</div>

    <template #footer>
      <div class="modal-actions">
        <NButton v-if="library" type="error" secondary :disabled="busy" @click="emit('remove', library)">{{ t('common.delete') }}</NButton>
        <NButton v-else-if="step !== 'source'" :disabled="busy" @click="step = step === 'details' ? 'folder' : 'source'">{{ t('settings.library.back') }}</NButton>
        <span class="spacer" />
        <NButton @click="show = false">{{ t('common.cancel') }}</NButton>
        <NButton v-if="step === 'folder'" type="primary" :disabled="!!currentBlocked || loadingFolders" :title="currentBlocked || ''" @click="chooseCurrent">
          {{ page?.path ? t('settings.library.selectNamed', { name: page.path.split('/').filter(Boolean).pop() || '/' }) : t('settings.library.select') }}
        </NButton>
        <NButton v-else-if="step === 'details'" type="primary" :loading="busy" @click="save">{{ library ? t('common.save') : t('settings.libraries.addShort') }}</NButton>
      </div>
    </template>
  </NModal>
  <PluginSettingsDialog v-model="settingsOpen" :plugin-id="settingsPlugin" @saved="emit('sources-changed')" />
</template>
