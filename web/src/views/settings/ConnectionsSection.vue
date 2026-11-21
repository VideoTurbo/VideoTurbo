<script setup lang="ts">
// 媒体服务器、通知或网络存储（WebDAV、Samba）的配置列表：可以添加多份，各自启用、测试、编辑。操作立即保存，不计入“未保存的更改”。
// 所有配置共用的插件设置在“插件”页的插件卡片上设置。
import { NButton, NInput, NModal, NSelect, NSwitch, type SelectOption } from 'naive-ui'
import { computed, h, onActivated, onMounted, reactive, ref } from 'vue'
import { api, errorMessage, type ConnectionKind, type NoticeMinLevel, type PluginConnection, type PluginType } from '@/api'
import AppIcon from '@/components/AppIcon.vue'
import FormField from '@/components/FormField.vue'
import PluginFields from '@/components/PluginFields.vue'
import SwitchRow from '@/components/SwitchRow.vue'
import { confirm, toast } from '@/lib/feedback'
import { hostOf, shortDate } from '@/lib/format'
import { initialValues, payloadValues as fieldPayload, savedSecrets, type FieldValues } from '@/lib/pluginFields'
import { openSettings } from '@/stores/navigation'
import { t } from '@/i18n'

const props = defineProps<{ kind: ConnectionKind; title: string; lead: string; empty: string }>()
const nouns: Record<ConnectionKind, { noun: string; example: string; missing: string }> = {
  'media-servers': { noun: t('settings.connections.mediaServer'), example: t('settings.connections.mediaServerPlaceholder'), missing: t('settings.connections.mediaServerMissing') },
  notifiers: { noun: t('settings.connections.notifier'), example: t('settings.connections.notifierPlaceholder'), missing: t('settings.connections.notifierMissing') },
  'media-sources': { noun: t('settings.connections.storage'), example: t('settings.connections.storagePlaceholder'), missing: t('settings.connections.storageMissing') },
}
const noun = computed(() => nouns[props.kind].noun)
const levelOptions: { value: NoticeMinLevel; label: string }[] = [
  { value: 'error', label: t('settings.connections.level.error') },
  { value: 'warning', label: t('settings.connections.level.warning') },
  { value: 'info', label: t('settings.connections.level.info') },
]

const items = ref<PluginConnection[]>([])
const types = ref<PluginType[]>([])
const loadError = ref('')

async function load(): Promise<void> {
  try {
    const [list, typeList] = await Promise.all([api.connections(props.kind), api.connectionTypes(props.kind)])
    items.value = list
    types.value = typeList
    loadError.value = ''
  } catch (error) {
    loadError.value = errorMessage(error)
  }
}
onMounted(load)
onActivated(load)

// ---- 编辑表单 ----
/** 正在编辑的配置；'new' 表示添加，null 表示关闭。 */
const editing = ref<PluginConnection | 'new' | null>(null)
const existing = computed(() => (editing.value && editing.value !== 'new' ? editing.value : null))
const form = reactive({ type: '', name: '', enabled: true, minLevel: 'warning' as NoticeMinLevel, values: {} as FieldValues })
const result = ref<{ ok: boolean; text: string } | null>(null)
const busy = ref(false)

const schema = computed(() => types.value.find((item) => item.id === form.type) || null)
const typeOptions = computed(() => types.value.map((item) => ({ value: item.id, label: item.label, icon: item.icon })))
/** 类型下拉框的选项前显示图标。 */
function renderTypeLabel(option: SelectOption) {
  const icon = typeof option.icon === 'string' ? option.icon : ''
  return h('span', { class: 'select-icon-label' }, [
    icon ? h('img', { src: icon, alt: '' }) : null,
    String(option.label ?? ''),
  ])
}
/** 密钥类字段已保存过时不回显，留空表示不修改。 */
const saved = computed(() => savedSecrets(schema.value?.fields || [], existing.value?.values))

function resetValues(): void {
  form.values = initialValues(schema.value?.fields || [], existing.value?.values)
}

function openEditor(item: PluginConnection | null): void {
  editing.value = item || 'new'
  form.type = item?.type || types.value[0]?.id || ''
  form.name = item?.name || (item ? '' : schema.value?.label || '')
  form.enabled = item?.enabled ?? true
  form.minLevel = item?.min_level || 'warning'
  result.value = null
  resetValues()
}

function changeType(type: string): void {
  const previous = schema.value?.label
  form.type = type
  // 名称还是默认值时跟着类型变。
  if (!form.name || form.name === previous) form.name = schema.value?.label || ''
  resetValues()
}

function payloadValues(): FieldValues {
  return fieldPayload(schema.value?.fields || [], form.values)
}

function payload() {
  return {
    name: form.name.trim(),
    enabled: form.enabled,
    values: payloadValues(),
    ...(props.kind === 'notifiers' ? { min_level: form.minLevel } : {}),
  }
}

async function test(): Promise<void> {
  busy.value = true
  result.value = { ok: true, text: props.kind === 'notifiers' ? t('settings.connections.sendingTest') : t('compute.providers.testing') }
  try {
    const body = existing.value
      ? await api.testConnection(props.kind, existing.value.id, payloadValues())
      : await api.testConnectionType(props.kind, form.type, payloadValues())
    result.value = { ok: true, text: body.message }
  } catch (error) {
    result.value = { ok: false, text: errorMessage(error) }
  } finally {
    busy.value = false
  }
}

async function save(): Promise<void> {
  if (!form.name.trim()) return void (result.value = { ok: false, text: t('compute.providers.nameRequired') })
  busy.value = true
  try {
    if (existing.value) await api.updateConnection(props.kind, existing.value.id, payload())
    else await api.createConnection(props.kind, { ...payload(), type: form.type })
    editing.value = null
    toast.ok(t('settings.connections.saved', { noun: noun.value }))
    await load()
  } catch (error) {
    result.value = { ok: false, text: t('compute.providers.saveFailed', { error: errorMessage(error) }) }
  } finally {
    busy.value = false
  }
}

async function remove(item: PluginConnection): Promise<void> {
  if (!await confirm(t('settings.connections.deleteConfirm', { name: item.name }),
    { title: t('settings.connections.deleteTitle', { noun: noun.value }), confirmLabel: t('common.delete'), danger: true })) return
  try {
    await api.deleteConnection(props.kind, item.id)
    editing.value = null
    toast.ok(t('compute.providers.deleted'))
    await load()
  } catch (error) {
    toast.error(t('common.deleteFailed'), error)
  }
}

async function toggle(item: PluginConnection, enabled: boolean): Promise<void> {
  try {
    await api.updateConnection(props.kind, item.id, { name: item.name, enabled, values: {} })
    item.enabled = enabled
  } catch (error) {
    toast.error(t('compute.providers.saveFailedShort'), error)
  }
}


function statusText(item: PluginConnection): string {
  if (item.last_error) return t('settings.connections.lastFailed', { time: shortDate(item.last_error_at), error: item.last_error })
  return item.last_ok_at ? t('settings.connections.lastOk', { time: shortDate(item.last_ok_at) }) : ''
}
</script>

<template>
  <div class="settings-section">
    <div class="section-head" style="margin: 0; align-items: center">
      <h3>{{ title }}</h3><span class="spacer" />
      <NButton size="small" type="primary" :disabled="!types.length" @click="openEditor(null)"><template #icon><AppIcon name="plus" /></template>{{ t('settings.libraries.addShort') }}</NButton>
    </div>
    <p class="lead">{{ lead }}</p>
    <div v-if="loadError" class="warning bad">{{ t('settings.connections.loadFailed', { error: loadError }) }}</div>
    <div v-else-if="!items.length" class="muted key-empty">{{ empty }}</div>
    <div v-else class="cloud-provider-list">
      <div v-for="item in items" :key="item.id" class="cloud-provider">
        <div class="cloud-provider-head wide">
          <span class="plugin-icon">
            <img v-if="item.type_icon" :src="item.type_icon" alt="">
            <span v-else>{{ (item.type_label || item.type).slice(0, 1).toUpperCase() }}</span>
          </span>
          <div class="cloud-provider-title">
            <b>{{ item.name }}</b>
            <span class="cloud-provider-subtitle">{{ item.type_label }}<template v-if="item.summary"> · {{ item.summary }}</template><template v-if="item.min_level"> · {{ levelOptions.find((option) => option.value === item.min_level)?.label }}</template></span>
          </div>
          <span v-if="!item.available" class="pill bad">{{ t('settings.connections.missingPlugin', { type: item.type }) }}</span>
          <span v-else class="pill" :class="{ good: item.enabled && !item.last_error, bad: item.enabled && !!item.last_error }">{{ item.enabled ? (item.last_error ? t('settings.connections.state.failed') : t('settings.connections.state.enabled')) : t('settings.connections.state.disabled') }}</span>
          <NSwitch :value="item.enabled" :disabled="!item.available" :aria-label="t('settings.libraries.enableName', { name: item.name })" @update:value="(value: boolean) => toggle(item, value)" />
          <NButton v-if="item.available" size="small" quaternary :aria-label="t('compute.providers.editLabelName', { name: item.name })" @click="openEditor(item)"><template #icon><AppIcon name="edit" /></template></NButton>
          <NButton v-else size="small" quaternary :aria-label="t('compute.providers.deleteName', { name: item.name })" @click="remove(item)"><template #icon><AppIcon name="trash" /></template></NButton>
        </div>
        <div v-if="!item.available" class="cloud-result bad">{{ t('settings.connections.pluginGone') }}</div>
        <div v-else-if="statusText(item)" class="cloud-result" :class="item.last_error ? 'bad' : 'good'">{{ statusText(item) }}</div>
      </div>
    </div>
    <span class="field-help">{{ t('settings.connections.missingHint', { missing: nouns[kind].missing }) }} <a href="#" @click.prevent="openSettings('plugins')">{{ t('settings.connections.pluginsLink') }}</a> {{ t('settings.connections.missingHintEnd') }}</span>

    <NModal :show="!!editing" preset="card" class="modal-card" :title="existing ? t('settings.connections.editTitle', { noun }) : t('settings.connections.addTitle', { noun })" @update:show="editing = null">
      <FormField :label="t('settings.connections.type')">
        <NSelect :value="form.type" :options="typeOptions" :render-label="renderTypeLabel" :disabled="!!existing" @update:value="changeType" />
        <span v-if="schema" class="cloud-provider-subtitle">{{ schema.description }}<template v-if="schema.homepage"> · <a :href="schema.homepage" target="_blank" rel="noopener noreferrer">{{ hostOf(schema.homepage) }}</a></template></span>
      </FormField>
      <FormField :label="t('compute.providers.name')" required><NInput v-model:value="form.name" maxlength="100" :placeholder="nouns[kind].example" /></FormField>
      <PluginFields v-model="form.values" :fields="schema?.fields || []" :saved="saved" />
      <FormField v-if="kind === 'notifiers'" :label="t('settings.connections.minLevel')" :tip="t('settings.connections.minLevelHelp')">
        <NSelect v-model:value="form.minLevel" :options="levelOptions" />
      </FormField>
      <SwitchRow v-model="form.enabled" :label="t('settings.library.enabled')" />
      <div v-if="result" class="cloud-result" :class="result.ok ? 'good' : 'bad'">{{ result.text }}</div>
      <template #footer>
        <div class="modal-actions">
          <NButton v-if="existing" type="error" secondary :disabled="busy" @click="remove(existing)">{{ t('common.delete') }}</NButton>
          <span class="spacer" />
          <NButton @click="editing = null">{{ t('common.cancel') }}</NButton>
          <NButton :disabled="busy" @click="test">{{ kind === 'notifiers' ? t('settings.connections.sendTest') : t('compute.providers.test') }}</NButton>
          <NButton type="primary" :disabled="busy" @click="save">{{ t('common.save') }}</NButton>
        </div>
      </template>
    </NModal>
  </div>
</template>
