<script setup lang="ts">
// 云平台授权：已添加的平台列表、添加 / 编辑表单。其他平台的插件在“设置 → 插件”中安装。
import { NButton, NInput, NModal, NSelect } from 'naive-ui'
import { computed, reactive, ref, watch } from 'vue'
import { api, errorMessage, type CloudOrphanProfile, type CloudProvider, type CloudProviderType } from '@/api'
import AppIcon from '@/components/AppIcon.vue'
import FormField from '@/components/FormField.vue'
import PluginFields from '@/components/PluginFields.vue'
import { confirm, toast } from '@/lib/feedback'
import { hostOf } from '@/lib/format'
import { initialValues, payloadValues as fieldPayload, savedSecrets, type FieldValues } from '@/lib/pluginFields'
import { instancesAt, refreshInstances } from '@/stores/cloud'
import { openSettings } from '@/stores/navigation'
import { refreshStatus } from '@/stores/status'
import { providersOpen } from './dialogs'
import { t } from '@/i18n'

const providers = ref<CloudProvider[]>([])
const types = ref<CloudProviderType[]>([])
const orphans = ref<CloudOrphanProfile[]>([])
const loadError = ref('')

async function load(): Promise<void> {
  try {
    const [providerList, typeList, orphanList] = await Promise.all([api.cloudProviders(), api.cloudProviderTypes(), api.cloudOrphanProviders()])
    providers.value = providerList
    types.value = typeList
    orphans.value = orphanList
    loadError.value = ''
  } catch (error) {
    loadError.value = errorMessage(error)
  }
}


// ---- 编辑表单 ----
/** 正在编辑的平台；null 表示在列表页，'new' 表示添加。 */
const editing = ref<CloudProvider | 'new' | null>(null)
const existing = computed(() => (editing.value && editing.value !== 'new' ? editing.value : null))
const form = reactive({ name: '', type: '', modelDir: '', machineId: '', values: {} as FieldValues })
const result = ref<{ ok: boolean; text: string } | null>(null)
const busy = ref(false)
const advancedOpen = ref(false)

/** 当前接入模式的字段定义；模式已被移除时退回到平台自带的信息。 */
const schema = computed<CloudProviderType | null>(() =>
  types.value.find((item) => item.id === form.type) || (existing.value as unknown as CloudProviderType | null))
const machines = computed(() => existing.value?.machines || [])
const machineOptions = computed(() => machines.value.map((item) => ({ value: item.id, label: `${item.host}:${item.port}` })))
const typeOptions = computed(() => types.value.map((item) => ({ value: item.id, label: item.label })))
const defaultModelDir = computed(() => schema.value?.default_model_dir || '$HOME/models')
/** 密钥类字段已保存过时不回显，留空表示不修改。 */
const saved = computed(() => {
  const keys = savedSecrets(schema.value?.fields || [], existing.value ? existing.value.values || {} : {})
  if (existing.value && form.machineId) keys.add('password')
  return keys
})

function resetValues(): void {
  form.values = initialValues(schema.value?.fields || [], existing.value?.values || {})
  if (machines.value.length) selectMachine(machines.value[0]!.id)
}

/** 一个 SSH 配置下有多台机器时，切换要编辑的那台。 */
function selectMachine(id: string): void {
  const machine = machines.value.find((item) => item.id === id)
  if (!machine) return
  form.machineId = id
  form.values = { ...form.values, host: machine.host || '', port: String(machine.port || 22), username: machine.username || 'root', password: '' }
}

function openEditor(provider: CloudProvider | null): void {
  editing.value = provider || 'new'
  form.name = provider?.label || ''
  form.type = provider?.type || provider?.id || types.value[0]?.id || ''
  form.modelDir = provider?.model_dir || ''
  form.machineId = ''
  advancedOpen.value = false
  result.value = null
  resetValues()
}

function payloadValues(): FieldValues {
  const values = fieldPayload(schema.value?.fields || [], form.values)
  // 云平台的配置沿用原有规则：留空的项不提交，表示不修改。
  for (const [key, value] of Object.entries(values)) if (value === '' || value === null) delete values[key]
  if (form.machineId) values.machine_id = form.machineId
  return values
}

async function test(): Promise<void> {
  busy.value = true
  result.value = { ok: true, text: t('compute.providers.testing') }
  try {
    const body = existing.value
      ? await api.testCloudProvider(existing.value.id, payloadValues())
      : await api.testCloudProviderType(form.type, payloadValues())
    result.value = { ok: true, text: body.message }
  } catch (error) {
    result.value = { ok: false, text: t('compute.providers.testFailed', { error: errorMessage(error) }) }
  } finally {
    busy.value = false
  }
}

/** 平台配置变了：重新读取列表，并让实例列表立即重查。 */
async function afterChange(done: string): Promise<void> {
  editing.value = null
  toast.ok(done)
  instancesAt.value = 0
  await Promise.all([load(), refreshStatus()])
  await refreshInstances()
}

async function save(): Promise<void> {
  const name = form.name.trim()
  if (!name) return void (result.value = { ok: false, text: t('compute.providers.nameRequired') })
  busy.value = true
  const payload = { name, model_dir: form.modelDir.trim(), values: payloadValues() }
  try {
    if (existing.value) await api.updateCloudProvider(existing.value.id, payload)
    else await api.createCloudProvider({ ...payload, type: form.type })
    await afterChange(t('compute.providers.saved'))
  } catch (error) {
    result.value = { ok: false, text: t('compute.providers.saveFailed', { error: errorMessage(error) }) }
  } finally {
    busy.value = false
  }
}

async function remove(): Promise<void> {
  const provider = existing.value
  if (!provider) return
  if (!await confirm(t('compute.providers.deleteConfirm', { label: provider.label }),
    { title: t('compute.providers.deleteTitle'), confirmLabel: t('compute.providers.delete'), danger: true })) return
  try {
    await api.deleteCloudProvider(provider.id)
    await afterChange(t('compute.providers.deleted'))
  } catch (error) {
    result.value = { ok: false, text: t('compute.providers.deleteFailedError', { error: errorMessage(error) }) }
  }
}

async function removeOrphan(profile: CloudOrphanProfile): Promise<void> {
  if (!await confirm(t('compute.providers.deleteOrphanConfirm', { label: profile.label }),
    { title: t('compute.providers.deleteTitle'), confirmLabel: t('compute.providers.delete'), danger: true })) return
  try {
    await api.deleteCloudProvider(profile.id)
    await afterChange(t('compute.providers.deleted'))
  } catch (error) {
    toast.error(t('compute.providers.deleteFailed'), error)
  }
}


function installPlugins(): void {
  providersOpen.value = false
  openSettings('plugins')
}

watch(providersOpen, (open) => {
  if (!open) return
  editing.value = null
  void load()
})

/** 关闭：在编辑页时先回到列表。 */
function close(): void {
  if (editing.value) editing.value = null
  else providersOpen.value = false
}
</script>

<template>
  <NModal :show="providersOpen" preset="card" class="modal-card" :title="editing ? (existing ? t('compute.providers.edit') : t('compute.providers.add')) : t('cloud.platform')" @update:show="close">
    <template v-if="!editing">
      <div class="section-head" style="margin: 0 0 16px; align-items: center">
        <h3>{{ t('compute.providers.added') }}</h3><span class="spacer" />
        <NButton size="small" type="primary" :disabled="!types.length" @click="openEditor(null)"><template #icon><AppIcon name="plus" /></template>{{ t('compute.providers.addButton') }}</NButton>
      </div>
      <div v-if="loadError" class="warning bad">{{ t('compute.providers.loadFailed', { error: loadError }) }}</div>
      <div v-else-if="!providers.length && !orphans.length" class="empty">{{ t('compute.providers.empty') }}</div>
      <div v-else class="cloud-provider-list">
        <div v-for="profile in orphans" :key="profile.id" class="cloud-provider">
          <div class="cloud-provider-head">
            <div class="cloud-provider-title">
              <b>{{ profile.label }}</b>
              <span class="cloud-provider-subtitle">{{ t('compute.providers.needsPlugin', { type: profile.type }) }}</span>
            </div>
            <span class="pill bad">{{ t('compute.providers.missingPlugin') }}</span>
            <NButton size="small" quaternary :aria-label="t('compute.providers.deleteLabel', { label: profile.label })" @click="removeOrphan(profile)"><template #icon><AppIcon name="trash" /></template></NButton>
          </div>
        </div>
        <div v-for="provider in providers" :key="provider.id" class="cloud-provider">
          <div class="cloud-provider-head">
            <div class="cloud-provider-title">
              <b>{{ provider.label }}</b>
              <span class="cloud-provider-subtitle">{{ provider.type_label || provider.label }}<template v-if="provider.homepage"> · <a :href="provider.homepage" target="_blank" rel="noopener noreferrer">{{ hostOf(provider.homepage) }}</a></template></span>
            </div>
            <span class="pill" :class="{ good: provider.configured }">{{ provider.configured ? t('compute.providers.loginSaved') : t('compute.providers.notConfigured') }}</span>
            <NButton size="small" quaternary :aria-label="t('compute.providers.editLabel', { label: provider.label })" @click="openEditor(provider)"><template #icon><AppIcon name="edit" /></template></NButton>
          </div>
          <div v-if="provider.last_error" class="cloud-result bad">{{ provider.last_error }}</div>
        </div>
      </div>

      <p class="cloud-provider-subtitle" style="margin: 16px 0 0">{{ t('compute.providers.builtinHint') }} <a href="#" @click.prevent="installPlugins">{{ t('compute.providers.pluginsLink') }}</a> {{ t('compute.providers.builtinHintEnd') }}</p>
    </template>

    <template v-else>
      <FormField :label="t('compute.providers.name')" required><NInput v-model:value="form.name" maxlength="100" :placeholder="t('compute.providers.namePlaceholder')" /></FormField>
      <FormField :label="t('compute.providers.mode')">
        <NSelect v-model:value="form.type" :options="typeOptions" :disabled="!!existing" @update:value="resetValues" />
        <span v-if="schema" class="cloud-provider-subtitle">{{ schema.label }}<template v-if="schema.homepage"> · <a :href="schema.homepage" target="_blank" rel="noopener noreferrer">{{ hostOf(schema.homepage) }}</a></template></span>
      </FormField>
      <FormField v-if="machines.length > 1" label="机器"><NSelect :value="form.machineId" :options="machineOptions" @update:value="selectMachine" /></FormField>
      <PluginFields v-model="form.values" :fields="schema?.fields || []" :saved="saved" />
      <details class="more cloud-advanced" :open="advancedOpen" @toggle="advancedOpen = ($event.target as HTMLDetailsElement).open">
        <summary>{{ t('compute.providers.advanced') }}</summary>
        <FormField :label="t('compute.providers.modelDir')" optional :tip="t('compute.providers.modelDirHelp', { defaultModelDir })">
          <NInput v-model:value="form.modelDir" maxlength="300" :placeholder="defaultModelDir" />
        </FormField>
      </details>
      <div v-if="result" class="cloud-result" :class="result.ok ? 'good' : 'bad'">{{ result.text }}</div>
    </template>

    <template v-if="editing" #footer>
      <div class="modal-actions">
        <NButton v-if="existing" type="error" secondary :disabled="busy" @click="remove">{{ t('common.delete') }}</NButton>
        <span class="spacer" />
        <NButton @click="editing = null">{{ t('common.cancel') }}</NButton>
        <NButton :disabled="busy" @click="test">{{ t('compute.providers.test') }}</NButton>
        <NButton type="primary" :disabled="busy" @click="save">{{ schema?.credential_mode === 'login' ? t('compute.providers.loginSave') : t('compute.providers.save') }}</NButton>
      </div>
    </template>
  </NModal>
</template>
