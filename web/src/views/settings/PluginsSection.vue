<script setup lang="ts">
// 插件页分两栏：“已安装”和“插件仓库”。
// 已安装：全部已安装的插件（云平台、媒体服务器、通知、媒体库来源）以卡片列出；一个插件可以同时提供多种类型。声明了插件设置的插件，卡片上有设置按钮。
// 按地址安装或本地上传的可以卸载；按地址安装的可以在线更新，本地上传的重新上传同一插件的文件即为更新。操作立即生效，不计入“未保存的更改”；
// 内置和 Python 包提供的插件随主控一起安装，只展示。
// 插件仓库：用户添加的仓库（仓库根目录的 plugins.json 列出插件），其中的插件按类型分组列出，提供多种类型的插件在每个类型下都出现。点“安装”走与按地址安装相同的流程。
// 安装和更新都分两步：先下载（或读取上传的文件）并静态检查（不执行），用户确认后才执行插件代码。确认框以插件信息为主，文件指纹（SHA-256）等技术信息折叠显示。
// 打开本页时自动检查按地址安装的插件（同样只下载、静态检查），有新内容才显示“更新”按钮。
import { NButton, NInput } from 'naive-ui'
import { computed, onActivated, onMounted, ref } from 'vue'
import { api, errorMessage, type InstalledPlugin, type PluginList, type PluginPreview, type PluginRepository, type RepositoryPlugin } from '@/api'
import AppIcon from '@/components/AppIcon.vue'
import { confirm } from '@/lib/feedback'
import PluginConfirmDialog, { type PluginConfirm } from './PluginConfirmDialog.vue'
import PluginSettingsDialog from './PluginSettingsDialog.vue'
import { shortDate } from '@/lib/format'
import { instancesAt, refreshInstances } from '@/stores/cloud'
import { isDesktop, refreshStatus } from '@/stores/status'
import { t } from '@/i18n'

const list = ref<PluginList | null>(null)
const loadError = ref('')
const url = ref('')
const busy = ref('')
const result = ref<{ ok: boolean; text: string } | null>(null)
const SOURCE_LABEL = { builtin: t('plugins.source.builtin'), package: t('plugins.source.package'), url: '', upload: t('plugins.source.upload') } as const
const removable = (plugin: InstalledPlugin) => plugin.source === 'url' || plugin.source === 'upload'
const versionText = (version?: string | null) => version ? `v${version.replace(/^v/i, '')}` : t('plugins.confirm.noVersion')

type Tab = 'installed' | 'repositories'
const tab = ref<Tab>('installed')

/** 有新内容的插件 → 新内容静态检查的结果。 */
const updates = ref<Record<string, PluginPreview>>({})
/** 检查更新失败的插件 → 原因。 */
const checkErrors = ref<Record<string, string>>({})
let checking = false

async function checkUpdates(): Promise<void> {
  const plugins = list.value && !list.value.safe_mode ? list.value.plugins.filter(plugin => plugin.source === 'url') : []
  if (checking) return
  checking = true
  const found: Record<string, PluginPreview> = {}
  const failed: Record<string, string> = {}
  await Promise.all(plugins.map(async (plugin) => {
    try {
      const check = await api.checkPluginUpdate(plugin.id)
      if (check.changed) found[plugin.id] = check
    } catch (error) {
      failed[plugin.id] = errorMessage(error)
    }
  }))
  updates.value = found
  checkErrors.value = failed
  checking = false
}

const hostOf = (address: string) => {
  try { return new URL(address).host } catch { return address }
}

async function load(): Promise<void> {
  try {
    list.value = await api.plugins()
    loadError.value = ''
    void checkUpdates()
  } catch (error) {
    loadError.value = errorMessage(error)
  }
}
onMounted(load)
onActivated(() => {
  void load()
  if (repositories.value) void loadRepositories()
})

/** 插件变了：云平台的接入模式和实例列表都可能变。 */
async function afterChange(text: string, kinds?: { id: string }[]): Promise<void> {
  result.value = { ok: true, text }
  await Promise.all([load(), refreshStatus()])
  if (!kinds || kinds.some(kind => kind.id === 'cloud')) {
    instancesAt.value = 0
    await refreshInstances()
  }
}

/** 运行一步操作；失败时显示原因并返回 null。 */
async function step<T>(key: string, action: () => Promise<T>): Promise<T | null> {
  busy.value = key
  result.value = null
  try {
    return await action()
  } catch (error) {
    result.value = { ok: false, text: errorMessage(error) }
    return null
  } finally {
    busy.value = ''
  }
}

const kindsText = (kinds: { label: string }[]) => kinds.map(kind => kind.label).join(t('common.listSeparator'))

/** 静态检查通过、等待用户确认安装或更新的插件。 */
const pending = ref<PluginConfirm | null>(null)

async function install(): Promise<void> {
  const address = url.value.trim()
  if (!address) return void (result.value = { ok: false, text: t('plugins.urlRequired') })
  const preview = await step('install', () => api.previewPlugin(address))
  if (preview) pending.value = { preview }
}

const fileInput = ref<HTMLInputElement | null>(null)

/** 读取选中的本地 .py 文件并静态检查；已从本地上传安装的同一插件按更新确认。 */
async function upload(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = '' // 允许再次选择同一个文件
  if (!file) return
  if (!/\.py$/i.test(file.name)) return void (result.value = { ok: false, text: t('plugins.fileRequired') })
  if (file.size > 1024 * 1024) return void (result.value = { ok: false, text: t('plugins.fileTooLarge') })
  let source: string
  try {
    source = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer())
  } catch {
    return void (result.value = { ok: false, text: t('plugins.fileUnreadable') })
  }
  const preview = await step('upload', () => api.previewUploadedPlugin(file.name, source))
  if (!preview) return
  const current = list.value?.plugins.find(plugin => plugin.id === preview.id && plugin.source === 'upload')
  pending.value = { preview, current, upload: { filename: preview.filename || file.name, source } }
}

function update(plugin: InstalledPlugin): void {
  const preview = updates.value[plugin.id]
  if (preview) pending.value = { preview, current: plugin }
}

const installedText = (body: { kinds?: { label: string }[]; label?: string; id: string; version?: string; sha256: string }) =>
  body.kinds?.length
    ? t('plugins.installedKinds', { kinds: kindsText(body.kinds), label: body.label || body.id, version: versionText(body.version) })
    : t('plugins.installed', { label: body.label || body.id, version: versionText(body.version) })

async function confirmed({ preview, current, upload: file }: PluginConfirm): Promise<void> {
  const iconSha256 = preview.icon_sha256 || ''
  if (file) {
    const body = await step('upload', () => api.installUploadedPlugin(file.filename, file.source, preview.sha256, iconSha256))
    pending.value = null
    if (!body) return
    const label = body.label || body.id
    const text = !body.changed ? t('plugins.upToDate', { label })
      : body.updated ? t('plugins.updated', { label, version: versionText(body.version) })
        : installedText(body)
    return afterChange(text, body.kinds)
  }
  if (!current) {
    const body = await step('install', () => api.installPlugin(preview.url, preview.sha256, iconSha256))
    pending.value = null
    if (!body) return
    url.value = ''
    return afterChange(installedText(body), body.kinds)
  }
  const body = await step(current.id, () => api.updatePlugin(current.id, preview.sha256, iconSha256))
  pending.value = null
  if (!body) return
  const label = body.label || current.label
  await afterChange(body.changed ? t('plugins.updated', { label, version: versionText(body.version) }) : t('plugins.upToDate', { label }), body.kinds ?? current.kinds)
}

// ---- 插件设置 ----
const settingsOpen = ref(false)
const settingsPlugin = ref<string | null>(null)

function openPluginSettings(plugin: InstalledPlugin): void {
  settingsPlugin.value = plugin.id
  settingsOpen.value = true
}

/** 正在使用这个插件的是什么：“3 个媒体库”或“3 份配置或媒体库”。 */
function usageWhat(plugin: InstalledPlugin): string {
  const onlySource = plugin.kinds.length === 1 && plugin.kinds[0].id === 'media_source'
  return onlySource ? t('plugins.usage.libraries', { count: plugin.usage }) : t('plugins.usage.configs', { count: plugin.usage })
}

function usageText(plugin: InstalledPlugin): string {
  return plugin.usage ? t('plugins.usage.inUse', { what: usageWhat(plugin) }) : t('plugins.usage.unused')
}

const AFFECTS: Record<string, string> = {
  cloud: t('plugins.affects.cloud'),
  media_server: t('plugins.affects.mediaServer'),
  media_source: t('plugins.affects.mediaSource'),
  notifier: t('plugins.affects.notifier'),
}

async function uninstall(plugin: InstalledPlugin): Promise<void> {
  const effects = plugin.kinds.map(kind => AFFECTS[kind.id]).filter(Boolean).join(t('common.listJoin'))
  const used = plugin.usage ? t('plugins.uninstallUsed', { what: usageWhat(plugin), effects }) : ''
  if (!await confirm(t('plugins.uninstallConfirm', { label: plugin.label, used }),
    { title: t('plugins.uninstallTitle'), confirmLabel: t('plugins.uninstall'), danger: true })) return
  const done = await step(plugin.id, () => api.uninstallPlugin(plugin.id))
  if (done === null) return
  await afterChange(t('plugins.uninstalled', { label: plugin.label }), plugin.kinds)
}

// ---- 插件仓库 ----
const repositories = ref<PluginRepository[] | null>(null)
const repositoriesError = ref('')
const repositoryUrl = ref('')
const loadingRepositories = ref(false)

async function loadRepositories(): Promise<void> {
  loadingRepositories.value = true
  try {
    repositories.value = (await api.pluginRepositories()).repositories
    repositoriesError.value = ''
  } catch (error) {
    repositoriesError.value = errorMessage(error)
  } finally {
    loadingRepositories.value = false
  }
}

function openTab(next: Tab): void {
  tab.value = next
  if (next === 'repositories' && !repositories.value && !loadingRepositories.value) void loadRepositories()
}

async function addRepository(): Promise<void> {
  const address = repositoryUrl.value.trim()
  if (!address) return void (result.value = { ok: false, text: t('plugins.repoUrlRequired') })
  const added = await step('repository', () => api.addPluginRepository(address))
  if (!added) return
  repositoryUrl.value = ''
  repositories.value = [...(repositories.value ?? []), added]
  const skipped = added.skipped ? t('plugins.repoSkipped', { skipped: added.skipped }) : ''
  result.value = { ok: true, text: t('plugins.repoAdded', { name: added.name, skipped, count: added.plugins.length }) }
}

async function removeRepository(repository: PluginRepository): Promise<void> {
  if (!await confirm(t('plugins.repoRemoveConfirm', { name: repository.name }),
    { title: t('plugins.repoRemoveTitle'), confirmLabel: t('plugins.repoRemove'), danger: true })) return
  const done = await step(`repository:${repository.id}`, () => api.removePluginRepository(repository.id))
  if (done === null) return
  repositories.value = (repositories.value ?? []).filter(item => item.id !== repository.id)
}

/** 仓库里的插件和它所属的仓库。 */
interface CatalogItem { plugin: RepositoryPlugin; repository: PluginRepository }

/** 按类型分组；提供多种类型的插件在每个类型下都出现。 */
const catalog = computed(() => {
  const kinds = list.value?.kinds ?? []
  const items = (repositories.value ?? []).flatMap(repository => repository.plugins.map(plugin => ({ plugin, repository })))
  return kinds
    .map(kind => ({ kind, items: items.filter(item => item.plugin.kinds.some(entry => entry.id === kind.id)) }))
    .filter(group => group.items.length)
})
const catalogCount = computed(() => (repositories.value ?? []).reduce((sum, repository) => sum + repository.plugins.length, 0))

/** GitHub 原始文件地址去掉分支部分再比较：仓库列表默认用 HEAD，按 README 安装的可能是 main。 */
function sourceKey(address = ''): string {
  try {
    const parsed = new URL(address)
    if (parsed.host === 'raw.githubusercontent.com') {
      const [owner, repo, , ...rest] = parsed.pathname.split('/').filter(Boolean)
      return `github:${owner}/${repo}/${rest.join('/')}`
    }
    return parsed.href
  } catch {
    return address
  }
}

/** 仓库里的插件与已安装插件的关系：未安装、已安装、可更新，或同一 id 已从别处安装（text 是说明）。 */
interface CatalogState { kind: 'available' | 'installed' | 'update' | 'other'; plugin?: InstalledPlugin; text?: string }

function stateOf(entry: RepositoryPlugin): CatalogState {
  const installed = list.value?.plugins.find(plugin => plugin.id === entry.id)
  if (!installed) return { kind: 'available' }
  if (installed.source === 'builtin' || installed.source === 'package') return { kind: 'other', plugin: installed, text: SOURCE_LABEL[installed.source] }
  if (installed.source === 'upload') return { kind: 'other', plugin: installed, text: t('plugins.installedFromUpload') }
  if (sourceKey(installed.url) !== sourceKey(entry.url)) return { kind: 'other', plugin: installed, text: t('plugins.installedFromUrl') }
  return updates.value[installed.id] ? { kind: 'update', plugin: installed } : { kind: 'installed', plugin: installed }
}

async function installFromRepository({ plugin, repository }: CatalogItem): Promise<void> {
  const preview = await step(`catalog:${plugin.id}`, () => api.previewPlugin(plugin.url))
  if (preview) pending.value = { preview, repository: { name: repository.name, sha256: plugin.sha256 } }
}
</script>

<template>
  <div class="settings-section">
    <div class="plugin-section-head">
      <h3>{{ t('plugins.title') }}</h3>
      <div class="segmented" role="tablist" :aria-label="t('plugins.title')">
        <button role="tab" :class="{ active: tab === 'installed' }" :aria-selected="tab === 'installed'" @click="openTab('installed')">
          {{ t('plugins.tab.installed') }}<span v-if="list" class="seg-count">{{ list.plugins.length }}</span>
        </button>
        <button role="tab" :class="{ active: tab === 'repositories' }" :aria-selected="tab === 'repositories'" @click="openTab('repositories')">
          {{ t('plugins.tab.repositories') }}<span v-if="repositories" class="seg-count">{{ catalogCount }}</span>
        </button>
      </div>
    </div>
    <div v-if="list?.safe_mode" class="warning bad">{{ t(isDesktop ? 'plugins.safeMode.desktop' : 'plugins.safeMode') }}</div>

    <template v-if="tab === 'installed'">
      <p class="lead">{{ t('plugins.lead') }}</p>
      <div class="cloud-plugin-install">
        <NInput v-model:value="url" maxlength="2000" placeholder="https://raw.githubusercontent.com/…/xxx.py" :disabled="!!busy || list?.safe_mode" @keyup.enter="install" />
        <NButton :loading="busy === 'install'" :disabled="!!busy || list?.safe_mode" @click="install">{{ t('plugins.install') }}</NButton>
        <NButton :loading="busy === 'upload'" :disabled="!!busy || list?.safe_mode" :title="t('plugins.uploadHelp')" @click="fileInput?.click()">
          <template #icon><AppIcon name="upload" /></template>{{ t('plugins.upload') }}
        </NButton>
        <input ref="fileInput" type="file" accept=".py,text/x-python" hidden @change="upload">
      </div>
      <div v-if="result" class="cloud-result" :class="result.ok ? 'good' : 'bad'">{{ result.text }}</div>
      <div v-if="loadError" class="warning bad">{{ t('plugins.loadFailed', { error: loadError }) }}</div>
      <template v-else-if="list">
        <div class="plugin-grid">
          <div v-for="plugin in list.plugins" :key="plugin.id" class="plugin-card" :class="{ broken: plugin.error }">
            <div class="plugin-card-head">
              <span class="plugin-icon">
                <img v-if="plugin.icon" :src="plugin.icon" alt="" draggable="false">
                <span v-else>{{ plugin.label.slice(0, 1).toUpperCase() }}</span>
              </span>
              <div class="plugin-card-title">
                <b :title="plugin.label">{{ plugin.label }}</b>
                <span class="plugin-tags">
                  <span v-for="kind in plugin.kinds" :key="kind.id" class="pill plain">{{ kind.label }}</span>
                  <span v-if="SOURCE_LABEL[plugin.source]" class="pill plain">{{ SOURCE_LABEL[plugin.source] }}</span>
                  <span v-if="plugin.settings_missing && !plugin.error" class="pill warn">{{ t('plugins.needsSettings') }}</span>
                </span>
              </div>
              <div v-if="removable(plugin) || plugin.has_settings" class="plugin-card-actions">
                <NButton v-if="updates[plugin.id]" size="tiny" type="primary" secondary :loading="busy === plugin.id" :disabled="!!busy || list.safe_mode" @click="update(plugin)">{{ t('plugins.update') }}</NButton>
                <NButton v-if="plugin.has_settings && !plugin.error" size="tiny" quaternary :aria-label="t('plugins.settingsName', { label: plugin.label })" :title="t('plugins.settingsName', { label: plugin.label })" @click="openPluginSettings(plugin)"><template #icon><AppIcon name="gear" /></template></NButton>
                <NButton v-if="removable(plugin)" size="tiny" quaternary :disabled="!!busy" :aria-label="t('plugins.uninstallName', { label: plugin.label })" :title="t('plugins.uninstallName', { label: plugin.label })" @click="uninstall(plugin)"><template #icon><AppIcon name="trash" /></template></NButton>
              </div>
            </div>
            <div class="plugin-card-meta">
              <span v-if="plugin.members.length > 1" :title="plugin.members.map(member => t('plugins.confirm.member', { label: member.label, kind: member.kind_label })).join('\n')">
                {{ t('plugins.membersSummary', { count: plugin.members.length, list: plugin.members.map(member => member.label).join(t('common.listSeparator')) }) }}
              </span>
              <template v-if="plugin.source === 'url'">
                <a :href="plugin.url" target="_blank" rel="noopener noreferrer" :title="plugin.url">{{ plugin.url }}</a>
                <span>{{ t('plugins.versionUpdated', { version: versionText(plugin.version), time: shortDate(plugin.updated_at) }) }}</span>
                <span v-if="updates[plugin.id]" class="plugin-update">{{ t('plugins.updateAvailable', { version: versionText(updates[plugin.id].version) }) }}</span>
                <span v-else-if="checkErrors[plugin.id]" :title="checkErrors[plugin.id]">{{ t('plugins.updateCheckFailed') }}</span>
              </template>
              <template v-else-if="plugin.source === 'upload'">
                <span :title="plugin.filename">{{ t('plugins.confirm.localFile', { filename: plugin.filename }) }}</span>
                <span>{{ t('plugins.versionUpdated', { version: versionText(plugin.version), time: shortDate(plugin.updated_at) }) }}</span>
              </template>
              <a v-else-if="plugin.homepage" :href="plugin.homepage" target="_blank" rel="noopener noreferrer">{{ hostOf(plugin.homepage) }}</a>
              <span>{{ usageText(plugin) }}</span>
            </div>
            <div v-if="plugin.error" class="cloud-result bad">{{ t('plugins.notLoaded', { error: plugin.error }) }}</div>
          </div>
        </div>
      </template>
    </template>

    <template v-else>
      <p class="lead">{{ t('plugins.repoLead') }}</p>
      <div class="cloud-plugin-install repository">
        <NInput v-model:value="repositoryUrl" maxlength="2000" :placeholder="t('plugins.repoPlaceholder')" :disabled="!!busy" @keyup.enter="addRepository" />
        <NButton :loading="busy === 'repository'" :disabled="!!busy" @click="addRepository">
          <template #icon><AppIcon name="plus" /></template>{{ t('plugins.repoAdd') }}
        </NButton>
      </div>
      <div v-if="result" class="cloud-result" :class="result.ok ? 'good' : 'bad'">{{ result.text }}</div>
      <div v-if="repositoriesError" class="warning bad">{{ t('plugins.repoLoadFailed', { error: repositoriesError }) }}</div>
      <template v-else-if="repositories">
        <div v-if="repositories.length" class="plugin-repositories">
          <div v-for="repository in repositories" :key="repository.id" class="plugin-repository">
            <div class="plugin-repository-info">
              <b>{{ repository.name }}</b>
              <a :href="repository.url" target="_blank" rel="noopener noreferrer" :title="repository.index_url">{{ repository.url }}</a>
              <span v-if="repository.error" class="plugin-repository-error" :title="repository.error">{{ repository.error }}</span>
              <span v-else>{{ t('plugins.repoCount', { count: repository.plugins.length }) }}</span>
            </div>
            <NButton size="tiny" quaternary :disabled="!!busy" :aria-label="t('plugins.repoRemoveName', { name: repository.name })" :title="t('plugins.repoRemoveName', { name: repository.name })" @click="removeRepository(repository)"><template #icon><AppIcon name="trash" /></template></NButton>
          </div>
          <NButton class="plugin-repositories-refresh" size="tiny" quaternary :loading="loadingRepositories" @click="loadRepositories"><template #icon><AppIcon name="refresh" /></template>{{ t('compute.instances.refresh') }}</NButton>
        </div>
        <div v-else class="empty-hint">{{ t('plugins.repoEmpty') }}</div>

        <section v-for="group in catalog" :key="group.kind.id" class="plugin-catalog-group">
          <h4>{{ group.kind.label }}<span>{{ group.items.length }}</span></h4>
          <div class="plugin-grid">
            <div v-for="item in group.items" :key="`${item.repository.id}:${item.plugin.id}`" class="plugin-card">
              <div class="plugin-card-head">
                <span class="plugin-icon">
                  <img v-if="item.plugin.icon" :src="item.plugin.icon" alt="" draggable="false">
                  <span v-else>{{ item.plugin.label.slice(0, 1).toUpperCase() }}</span>
                </span>
                <div class="plugin-card-title">
                  <b :title="item.plugin.label">{{ item.plugin.label }}</b>
                  <span class="plugin-tags">
                    <span v-for="kind in item.plugin.kinds" :key="kind.id" class="pill plain">{{ kind.label }}</span>
                    <span class="pill plain">{{ versionText(item.plugin.version) }}</span>
                  </span>
                </div>
                <div class="plugin-card-actions">
                  <template v-if="stateOf(item.plugin).kind === 'available'">
                    <NButton size="tiny" type="primary" secondary :loading="busy === `catalog:${item.plugin.id}`" :disabled="!!busy || list?.safe_mode" @click="installFromRepository(item)">
                      <template #icon><AppIcon name="download" /></template>{{ t('plugins.install') }}
                    </NButton>
                  </template>
                  <NButton v-else-if="stateOf(item.plugin).kind === 'update'" size="tiny" type="primary" secondary :loading="busy === item.plugin.id" :disabled="!!busy || list?.safe_mode" @click="update(stateOf(item.plugin).plugin as InstalledPlugin)">{{ t('plugins.update') }}</NButton>
                  <span v-else-if="stateOf(item.plugin).kind === 'installed'" class="pill plain">{{ t('plugins.tab.installed') }}</span>
                  <span v-else class="pill plain" :title="t('plugins.idTaken', { id: item.plugin.id })">{{ stateOf(item.plugin).text }}</span>
                </div>
              </div>
              <div class="plugin-card-meta">
                <span v-if="item.plugin.description" class="plugin-description" :title="item.plugin.description">{{ item.plugin.description }}</span>
                <span :title="item.plugin.url">{{ t('plugins.fromRepo', { name: item.repository.name }) }}</span>
                <a v-if="item.plugin.homepage" :href="item.plugin.homepage" target="_blank" rel="noopener noreferrer">{{ hostOf(item.plugin.homepage) }}</a>
              </div>
            </div>
          </div>
        </section>
      </template>
      <div v-else-if="loadingRepositories" class="empty-hint">{{ t('plugins.repoLoading') }}</div>
    </template>

    <PluginSettingsDialog v-model="settingsOpen" :plugin-id="settingsPlugin" @saved="load" />
    <PluginConfirmDialog v-model="pending" :loading="!!busy && busy === (pending?.upload ? 'upload' : pending?.current?.id ?? 'install')" @confirm="confirmed" />
  </div>
</template>
