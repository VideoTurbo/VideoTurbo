<script setup lang="ts">
// 系统设置。左侧分类，右侧表单；有改动时底部出现保存条，保存的是当前分类。
// 账户、GPU 机器密钥、媒体库、媒体服务器、通知和插件的操作立即保存，不计入“未保存的更改”。
import { NButton, NInput, NInputNumber, NSelect } from 'naive-ui'
import { computed, onActivated, reactive, ref, watch } from 'vue'
import { api, type AppConfig, type ConfigSection } from '@/api'
import AppIcon from '@/components/AppIcon.vue'
import type { IconName } from '@/components/icons'
import FormField from '@/components/FormField.vue'
import HelpTip from '@/components/HelpTip.vue'
import SwitchRow from '@/components/SwitchRow.vue'
import { locale, LOCALES, setLocale, t, type Locale } from '@/i18n'
import { confirm, toast } from '@/lib/feedback'
import { configSchemas, toConfigPayload, toFormValues, type FormValues } from '@/lib/settings'
import { loadConfig } from '@/stores/config'

import { settingsRequest, type SettingsCategory } from '@/stores/navigation'
import { isDesktop, refreshStatus } from '@/stores/status'
import AccountSection from './AccountSection.vue'
import ConfigForm from './ConfigForm.vue'
import ConnectionsSection from './ConnectionsSection.vue'
import LibrariesSection from './LibrariesSection.vue'
import PluginsSection from './PluginsSection.vue'
import WorkerTokenSection from './WorkerTokenSection.vue'

type Category = SettingsCategory
const navigation: { heading: string; items: { id: Category; label: string; icon: IconName }[] }[] = [
  { heading: t('settings.group.general'), items: [
    { id: 'media', label: t('settings.nav.media'), icon: 'folder' }, { id: 'media_servers', label: t('settings.nav.mediaServers'), icon: 'play' },
    { id: 'subtitle', label: t('settings.nav.subtitle'), icon: 'captions' }, { id: 'restore', label: t('settings.nav.restore'), icon: 'wand' },
    { id: 'cloud', label: t('settings.nav.cloud'), icon: 'cloud' }, { id: 'plugins', label: t('settings.nav.plugins'), icon: 'code' }, { id: 'ui', label: t('settings.nav.ui'), icon: 'monitor' },
  ] },
  { heading: t('settings.group.system'), items: [
    { id: 'notifiers', label: t('settings.nav.notifiers'), icon: 'bell' }, { id: 'system', label: t('settings.nav.system'), icon: 'file' }, { id: 'account', label: t('settings.nav.account'), icon: 'gear' },
  ] },
]
const category = ref<Category>('media')

// ---- 表单 ----
const emptySections = () => Object.fromEntries(Object.keys(configSchemas).map((key) => [key, {}])) as Record<ConfigSection, FormValues>
const form = reactive({
  sections: emptySections(),
  asr: { mode: 'whisperjav' as const, whisperjav_ensemble: false },
  uploadLimit: null as number | null,
  cloud: { idle_minutes: 30 as number | null, backstop_minutes: 60, update_jasna: true, jasna_license: '' },
  ui: { show_posters: true, notice_sound: true },
})
const saved = ref<AppConfig | null>(null)
const jasnaLicenseSet = ref(false)
/** 读取或保存后的表单快照；与当前表单不同即有未保存的更改。 */
const snapshot = ref('')
const dirty = computed(() => snapshot.value !== '' && JSON.stringify(form) !== snapshot.value)
const saving = ref(false)

async function load(): Promise<void> {
  try {
    const [config, asr, transfer, ui, cloud] = await Promise.all([loadConfig(), api.asrSettings(), api.transferSettings(), api.uiSettings(), api.cloudSettings()])
    saved.value = config
    for (const section of Object.keys(configSchemas) as ConfigSection[]) form.sections[section] = toFormValues(section, config.settings[section])
    form.asr = { mode: asr.mode, whisperjav_ensemble: asr.whisperjav_ensemble === true }
    form.uploadLimit = transfer.upload_limit_mb ?? null
    form.ui = { show_posters: ui.show_posters === true, notice_sound: ui.notice_sound !== false }
    form.cloud = { idle_minutes: cloud.idle_minutes, backstop_minutes: cloud.backstop_minutes, update_jasna: cloud.update_jasna !== false, jasna_license: '' }
    jasnaLicenseSet.value = cloud.jasna_license_set
    snapshot.value = JSON.stringify(form)
  } catch (error) {
    toast.error(t('settings.loadFailed'), error)
  }
}
onActivated(load)

// 其他页面请求打开某个分类（例如云平台对话框里的“安装插件”）。
watch(settingsRequest, (next) => {
  if (!next) return
  settingsRequest.value = null
  void switchCategory(next)
}, { immediate: true })

async function switchCategory(next: Category): Promise<void> {
  if (next === category.value) return
  if (dirty.value) {
    if (!await confirm(t('settings.discardConfirm'), { title: t('settings.discardTitle'), confirmLabel: t('settings.discardButton') })) return
    await load()
  }
  category.value = next
}

const languageTable = computed(() => saved.value?.languages || [])

// ---- 云主机 ----
const backstopOptions = computed(() => {
  const choices = [0, 10, 30, 60, 120]
  // 已保存的值不在常用档位里时也要能显示。
  if (!choices.includes(form.cloud.backstop_minutes)) choices.push(form.cloud.backstop_minutes)
  return choices.map((value) => ({ value, label: value === 0 ? t('settings.backstop.off') : value === 60 ? t('settings.backstop.default') : t('settings.backstop.minutes', { value }) }))
})

async function clearJasnaLicense(): Promise<void> {
  if (!await confirm(t('settings.clearLicenseConfirm'),
    { title: t('settings.clearLicenseTitle'), confirmLabel: t('settings.clearLicenseButton'), danger: true })) return
  try {
    jasnaLicenseSet.value = (await api.clearJasnaLicense()).jasna_license_set
    toast.ok(t('settings.licenseCleared'))
    await refreshStatus()
  } catch (error) {
    toast.error(t('settings.clearFailed'), error)
  }
}

function saveCloud(): Promise<unknown> {
  const idle = form.cloud.idle_minutes
  if (idle == null || !Number.isInteger(idle) || idle < 3 || idle > 240) throw new Error(t('settings.idleRange'))
  return api.saveCloudSettings({ ...form.cloud, idle_minutes: idle, jasna_license: form.cloud.jasna_license.trim() || null })
}

// ---- 保存：每个分类保存自己拥有的设置 ----
const saveSection = (section: ConfigSection) =>
  api.saveConfigSection(section, toConfigPayload(section, form.sections[section]))

const savers: Partial<Record<Category, () => Promise<unknown>>> = {
  media: () => saveSection('scan'),
  subtitle: async () => {
    // 先转换并校验两个配置分区，避免只保存了一半。
    const subtitles = toConfigPayload('subtitles', form.sections.subtitles)
    const translation = toConfigPayload('translation', form.sections.translation)
    await api.saveConfigSection('subtitles', subtitles)
    await api.saveAsrSettings(form.asr)
    await api.saveConfigSection('translation', translation)
  },
  restore: saveCloud,
  system: () => saveSection('server'),
  cloud: async () => {
    if (form.uploadLimit !== null && !(form.uploadLimit > 0)) throw new Error(t('settings.uploadLimitPositive'))
    await saveCloud() // 先校验并保存云主机参数，避免只保存了一半
    await api.saveTransferSettings(form.uploadLimit)
  },
  ui: () => api.saveUiSettings(form.ui),
}

async function save(): Promise<void> {
  saving.value = true
  try {
    await savers[category.value]?.()
    toast.ok(t('settings.saved'))
    await Promise.all([load(), refreshStatus()])
  } catch (error) {
    toast.error(t('compute.providers.saveFailedShort'), error)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <section class="pane">
    <section class="card settings">
      <nav class="settings-nav" :aria-label="t('settings.nav.label')">
        <template v-for="group in navigation" :key="group.heading">
          <span class="settings-nav-heading">{{ group.heading }}</span>
          <button v-for="item in group.items" :key="item.id" :class="{ active: category === item.id }" @click="switchCategory(item.id)">
            <AppIcon :name="item.icon" />{{ item.label }}
          </button>
        </template>
      </nav>

      <div class="settings-body">
        <template v-if="category === 'media'">
          <LibrariesSection :languages="languageTable" />
          <div class="settings-section">
            <h3>{{ t('settings.scan') }}</h3>
            <ConfigForm v-model="form.sections.scan" :fields="configSchemas.scan" />
          </div>
        </template>

        <ConnectionsSection
          v-else-if="category === 'media_servers'" kind="media-servers" :title="t('settings.nav.mediaServers')"
          lead=""
          :empty="t('settings.mediaServersEmpty')"
        />

        <template v-else-if="category === 'subtitle'">
          <div class="settings-section">
            <h3>{{ t('settings.subtitleRules') }}</h3>
            <ConfigForm v-model="form.sections.subtitles" :fields="configSchemas.subtitles" />
          </div>
          <div class="settings-section">
            <h3>{{ t('settings.asr') }}</h3>
            <SwitchRow
              v-model="form.asr.whisperjav_ensemble" :label="t('settings.dualPass')"
              :tip="t('settings.dualPassHelp')"
              :help="t('settings.dualPassTip')"
            />
          </div>
          <div class="settings-section">
            <h3>{{ t('settings.translation') }}</h3>
            <ConfigForm v-model="form.sections.translation" :fields="configSchemas.translation" />
          </div>
        </template>

        <div v-else-if="category === 'restore'" class="settings-section">
          <h3>Jasna</h3>
          <p class="lead">{{ t('settings.restoreLead') }}</p>
          <FormField :label="t('settings.jasnaLicense')" optional :tip="t('settings.jasnaLicenseHelp')">
            <div class="inline-form">
              <NInput v-model:value="form.cloud.jasna_license" type="password" :input-props="{ autocomplete: 'off' }" :placeholder="jasnaLicenseSet ? t('plugins.fields.savedKeep') : t('settings.jasnaLicenseEmpty')" />
              <NButton v-if="jasnaLicenseSet" @click="clearJasnaLicense">{{ t('settings.clear') }}</NButton>
            </div>
          </FormField>
          <SwitchRow
            v-model="form.cloud.update_jasna" :label="t('settings.jasnaUpdate')" :help="t('settings.jasnaUpdateHelp')"
            :tip="t('settings.jasnaUpdateTip')"
          />
        </div>

        <template v-else-if="category === 'cloud'">
          <div class="settings-section">
            <h3>{{ t('settings.autoShutdown') }}</h3>
            <p class="lead">{{ t('settings.autoShutdownLead') }}</p>
            <div class="field-row">
              <FormField :label="t('settings.idleMinutes')" :tip="t('settings.idleMinutesHelp')">
                <NInputNumber v-model:value="form.cloud.idle_minutes" :min="3" :max="240" :precision="0" />
              </FormField>
              <FormField :label="t('settings.backstop')" :tip="t('settings.backstopHelp')">
                <NSelect v-model:value="form.cloud.backstop_minutes" :options="backstopOptions" />
              </FormField>
            </div>
          </div>
          <WorkerTokenSection />
          <div class="settings-section">
            <h3>{{ t('settings.transfer') }}</h3>
            <p class="lead">{{ t('settings.transferLead') }}</p>
            <FormField :label="t('settings.uploadLimit')" optional :tip="t('settings.uploadLimitHelp')">
              <NInputNumber v-model:value="form.uploadLimit" :min="0.1" :step="0.1" clearable :placeholder="t('settings.uploadLimitPlaceholder')" />
            </FormField>
          </div>
        </template>

        <div v-else-if="category === 'ui'" class="settings-section">
          <h3>{{ t('settings.nav.ui') }}</h3>
          <FormField :label="t('settings.ui.language')" :help="t('settings.ui.languageHelp')">
            <NSelect :value="locale" :options="LOCALES.map((item) => ({ label: item.label, value: item.id }))" style="max-width: 240px" @update:value="(value: Locale) => value !== locale && setLocale(value)" />
          </FormField>
          <SwitchRow v-model="form.ui.notice_sound" :label="t('settings.ui.noticeSound')" :help="t('settings.ui.noticeSoundHelp')" />
          <SwitchRow
            v-model="form.ui.show_posters" :label="t('settings.ui.posters')" :help="t('settings.ui.postersHelp')"
            :tip="t('settings.ui.postersTip')"
          />
        </div>

        <div v-else-if="category === 'system'" class="settings-section">
          <h3>{{ t('settings.nav.system') }}</h3>
          <ConfigForm v-model="form.sections.server" :fields="configSchemas.server" />
          <div v-if="saved" class="readonly-list">
            <div class="readonly-title">{{ t(isDesktop ? 'settings.deployment.desktop' : 'settings.deployment') }}</div>
            <div class="readonly-row"><span>{{ t('settings.port') }}<HelpTip :text="t(isDesktop ? 'settings.portTip.desktop' : 'settings.portTip')" /></span><code>{{ saved.deployment.port }}</code></div>
            <div class="readonly-row"><span>{{ t('settings.workerPort') }}<HelpTip :text="t(isDesktop ? 'settings.portTip.desktop' : 'settings.portTip')" /></span><code>{{ saved.deployment.worker_port }}</code></div>
            <div class="readonly-row"><span>{{ t('settings.backupDir') }}</span><code>{{ saved.deployment.restore_backup_dir || t('settings.backupDirUnset') }}</code></div>
          </div>
          <div class="readonly-list" style="margin-top: 12px">
            <div class="readonly-title">{{ t('settings.diagnostics') }}</div>
            <div style="padding: 10px 14px 12px">
              <p class="lead" style="margin-bottom: 10px">{{ t('settings.diagnosticsHelp') }}</p>
              <NButton size="small" tag="a" :href="api.diagnosticsUrl" download><template #icon><AppIcon name="download" /></template>{{ t('settings.diagnosticsDownload') }}</NButton>
            </div>
          </div>
        </div>

        <ConnectionsSection
          v-else-if="category === 'notifiers'" kind="notifiers" :title="t('settings.nav.notifiers')"
          lead=""
          :empty="t('settings.notifiersEmpty')"
        />

        <PluginsSection v-else-if="category === 'plugins'" />

        <AccountSection v-else-if="category === 'account'" />

        <div v-if="dirty" class="settings-footer">
          <span class="footer-note">{{ t('settings.unsaved') }}</span>
          <NButton :disabled="saving" @click="load">{{ t('settings.discard') }}</NButton>
          <NButton type="primary" :loading="saving" @click="save">{{ t('common.save') }}</NButton>
        </div>
      </div>
    </section>
  </section>
</template>
