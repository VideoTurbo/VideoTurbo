<script setup lang="ts">
// 翻译节点的添加 / 编辑表单。
import { NButton, NInput, NInputNumber, NModal, NSelect } from 'naive-ui'
import { computed, reactive, ref, watch } from 'vue'
import { api, errorMessage, type NodeAuth, type NodePayload, type NodePool, type NodeProtocol } from '@/api'
import AppIcon from '@/components/AppIcon.vue'
import FormField from '@/components/FormField.vue'
import SwitchRow from '@/components/SwitchRow.vue'
import { confirm, toast } from '@/lib/feedback'
import { fullDate } from '@/lib/format'
import { config } from '@/stores/config'
import { refreshStatus } from '@/stores/status'
import type { NodeEditorTarget } from './types'
import { t } from '@/i18n'

const target = defineModel<NodeEditorTarget | null>({ required: true })
const emit = defineEmits<{ saved: [] }>()

const poolOptions = [{ value: 'translation', label: t('llm.pool.translation') }, { value: 'fallback', label: t('llm.pool.fallback') }]
const protocolOptions = [
  { value: 'openai_responses', label: 'OpenAI Responses' }, { value: 'openai_chat', label: 'OpenAI Chat Completions' }, { value: 'vertex_ai', label: 'Vertex AI' },
]
const authOptions = [{ value: 'api_key', label: 'API Key' }, { value: 'service_account', label: t('llm.editor.serviceAccount') }]
const effortOptions = [{ value: '', label: t('llm.editor.default') }, ...['none', 'low', 'medium', 'high'].map((value) => ({ value, label: value }))]

const form = reactive({
  name: '', pool: 'translation' as NodePool, protocol: 'openai_responses' as NodeProtocol, auth: 'api_key' as NodeAuth,
  baseUrl: '', vertexUrl: '', project: '', location: 'global', key: '', model: '', concurrency: 2 as number | null, effort: '', enabled: true,
})
/** 刚选择的服务账号 JSON 文件。 */
const serviceAccount = ref<{ text: string; email: string; project: string } | null>(null)
const saving = ref(false)

const editing = computed(() => (target.value && 'node' in target.value ? target.value.node : null))
/** Key 留空时沿用哪个已保存节点的 Key：正在编辑的节点，或“复制为新节点”的来源。 */
const keySource = computed(() => editing.value || (target.value && 'template' in target.value ? target.value.template : null) || null)

const vertex = computed(() => form.protocol === 'vertex_ai')
const useServiceAccount = computed(() => vertex.value && form.auth === 'service_account')

/** 接口连接信息。Vertex AI 用 API Key 时项目 ID 和自定义地址可选；用服务账号时项目 ID 取自文件。 */
const connection = computed(() => {
  const auth: NodeAuth = vertex.value ? form.auth : 'api_key'
  const keyAuth = vertex.value && auth === 'api_key'
  const url = vertex.value ? (keyAuth ? form.vertexUrl : '') : form.baseUrl
  return {
    protocol: form.protocol, auth, base_url: url.trim() || null,
    project: keyAuth ? form.project.trim() || null : null,
    location: vertex.value ? form.location.trim() || 'global' : 'global',
  }
})
/** 表单里新填的凭据：API Key，或刚选择的服务账号 JSON 文本。 */
const typedKey = computed(() => (useServiceAccount.value ? serviceAccount.value?.text || '' : form.key.trim()))
/** 已保存的凭据只有认证方式一致时才能沿用。 */
const savedKey = computed(() => {
  const source = keySource.value
  if (!source?.key_configured) return null
  return (source.protocol === 'vertex_ai' ? source.auth : 'api_key') === connection.value.auth ? source : null
})
const connectionReady = computed(() => (vertex.value || !!connection.value.base_url) && (!!typedKey.value || !!savedKey.value))

const serviceAccountStatus = computed(() => {
  if (serviceAccount.value) return t('llm.editor.accountSelected', { email: serviceAccount.value.email, project: serviceAccount.value.project })
  return savedKey.value ? t('llm.editor.accountSaved', { key: savedKey.value.key_masked || '', project: savedKey.value.project || '' }) : t('llm.editor.noFile')
})

async function pickServiceAccount(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  try {
    const text = await file.text()
    const info = JSON.parse(text) as Record<string, string>
    if (info?.type !== 'service_account' || !info.client_email || !info.private_key || !info.project_id) throw new Error(t('llm.editor.notServiceAccount'))
    serviceAccount.value = { text, email: info.client_email, project: info.project_id }
  } catch (error) {
    toast.fail(t('llm.editor.fileUnusable', { error: error instanceof SyntaxError ? t('llm.editor.invalidJson') : errorMessage(error) }))
  }
}

// ---- 模型列表：按连接信息缓存，地址、协议或凭据来源变了就作废 ----
const models = reactive({ list: [] as string[], fetchedAt: null as string | null, error: '', signature: '' })
const fetching = ref(false)
const signature = computed(() => {
  const c = connection.value
  return [c.protocol, c.auth, c.base_url, c.project, c.location, useServiceAccount.value ? serviceAccount.value?.email || '' : ''].join('|')
})
watch(signature, (next) => {
  if (next !== models.signature) Object.assign(models, { list: [], fetchedAt: null, error: '', signature: next })
})
const modelOptions = computed(() => models.list.map((value) => ({ value, label: value })))
const modelsStatus = computed(() => {
  if (!connectionReady.value) return t('llm.editor.modelsNeedConnection')
  if (models.error) return t('llm.editor.modelsFailed', { error: models.error })
  return t('llm.editor.modelCount', { count: models.list.length }) + (models.fetchedAt ? t('llm.editor.updatedAt', { time: fullDate(models.fetchedAt) }) : t('llm.editor.notFetched'))
})

async function fetchModels(silent = false): Promise<void> {
  if (fetching.value || !connectionReady.value) return
  const requested = signature.value
  fetching.value = true
  try {
    const body = await api.listModels({ node_id: keySource.value?.id ?? null, ...connection.value, api_key: typedKey.value || null })
    if (requested !== signature.value) return
    Object.assign(models, { list: body.models, fetchedAt: new Date().toISOString(), error: '' })
    if (!silent) toast.ok(t('llm.editor.modelsLoaded', { count: body.models.length }))
  } catch (error) {
    if (requested !== signature.value) return
    models.error = errorMessage(error)
    if (!silent) toast.fail(t('llm.editor.modelsHint', { error: models.error }))
  } finally {
    fetching.value = false
  }
}

/** 首次展开且没有缓存时自动拉取一次；失败不打扰，可在列表底部看到原因。 */
function onModelMenu(show: boolean): void {
  if (show && !models.list.length && !models.fetchedAt && !models.error) void fetchModels(true)
}
function onModelPicked(value: string | null): void {
  if (value && !form.name.trim()) form.name = value
}

// ---- 打开 / 保存 / 删除 ----
watch(target, (next) => {
  if (!next) return
  const base = 'node' in next ? next.node : next.template
  const isVertex = base?.protocol === 'vertex_ai'
  Object.assign(form, {
    name: 'node' in next ? next.node.name : base ? t('llm.editor.copyName', { name: base.name }) : '',
    pool: 'node' in next ? next.node.pool : next.pool,
    protocol: base?.protocol || 'openai_responses', auth: isVertex ? base?.auth || 'api_key' : 'api_key',
    baseUrl: isVertex ? '' : base?.base_url || '', vertexUrl: isVertex ? base?.base_url || '' : '',
    project: base?.project || '', location: base?.location || 'global', key: '', model: base?.model || '',
    concurrency: base?.concurrency || 2, effort: base?.reasoning_effort ?? '', enabled: base?.enabled !== false,
  })
  serviceAccount.value = null
  Object.assign(models, { list: base?.models || [], fetchedAt: base?.models_fetched_at || null, error: '', signature: signature.value })
})

function close(): void {
  target.value = null
}

async function afterChange(message: string): Promise<void> {
  toast.ok(message)
  close()
  emit('saved')
  await refreshStatus()
}

async function save(): Promise<void> {
  const payload: NodePayload = {
    name: form.name.trim(), pool: form.pool, ...connection.value, api_key: typedKey.value || null,
    copy_key_from: editing.value ? null : keySource.value?.id ?? null,
    model: (form.model || '').trim(), concurrency: Number(form.concurrency), reasoning_effort: form.effort || null, enabled: form.enabled,
  }
  if (!payload.name) return toast.fail(t('compute.providers.nameRequired'))
  if (!vertex.value && !payload.base_url) return toast.fail(t('llm.editor.urlRequired'))
  if (!payload.api_key && !savedKey.value) return toast.fail(useServiceAccount.value ? t('llm.editor.accountRequired') : t('llm.editor.keyRequired'))
  if (!payload.model) return toast.fail(t('llm.editor.modelRequired'))
  if (!Number.isInteger(payload.concurrency) || payload.concurrency < 1 || payload.concurrency > 8) return toast.fail(t('llm.editor.concurrencyRange'))
  saving.value = true
  try {
    if (editing.value) await api.updateNode(editing.value.id, payload)
    else await api.createNode(payload)
    await afterChange(t('llm.editor.saved', { name: payload.name }))
  } catch (error) {
    toast.error(t('llm.editor.saveFailed'), error)
  } finally {
    saving.value = false
  }
}

async function remove(): Promise<void> {
  const node = editing.value
  if (!node) return
  if (!await confirm(t('llm.editor.deleteConfirm', { name: node.name }), { title: t('llm.editor.deleteTitle'), confirmLabel: t('llm.editor.delete'), danger: true })) return
  try {
    await api.deleteNode(node.id)
    await afterChange(t('llm.editor.deleted'))
  } catch (error) {
    toast.error(t('common.deleteFailed'), error)
  }
}

// 配置还没读到时（直接从别处打开）不显示表单。
const ready = computed(() => !!config.value)
</script>

<template>
  <NModal :show="target !== null && ready" preset="card" class="modal-card" :title="editing ? t('llm.editor.editTitle', { name: editing.name }) : t('llm.add')" @update:show="close">
    <div class="field-row">
      <FormField :label="t('compute.providers.name')" required><NInput v-model:value="form.name" maxlength="60" :placeholder="t('llm.editor.namePlaceholder')" /></FormField>
      <FormField :label="t('llm.editor.pool')" :tip="t('llm.editor.poolHelp')"><NSelect v-model:value="form.pool" :options="poolOptions" /></FormField>
    </div>
    <FormField :label="t('llm.editor.protocol')" :tip="t('llm.editor.protocolHelp')">
      <NSelect v-model:value="form.protocol" :options="protocolOptions" />
    </FormField>

    <FormField v-if="!vertex" :label="t('llm.editor.url')" required :tip="t('llm.editor.urlHelp')" :help="t('llm.editor.urlPlaceholder')">
      <NInput v-model:value="form.baseUrl" placeholder="https://api.example.com/v1" />
    </FormField>
    <template v-else>
      <div class="field-row" :class="{ three: !useServiceAccount }">
        <FormField :label="t('llm.editor.auth')" :tip="t('llm.editor.authHelp')">
          <NSelect v-model:value="form.auth" :options="authOptions" />
        </FormField>
        <FormField v-if="!useServiceAccount" :label="t('llm.editor.project')" optional :tip="t('llm.editor.projectHelp')">
          <NInput v-model:value="form.project" :placeholder="t('llm.editor.usuallyEmpty')" />
        </FormField>
        <FormField :label="t('compute.instances.col.region')" :tip="t('llm.editor.regionHelp')"><NInput v-model:value="form.location" placeholder="global" /></FormField>
      </div>
      <FormField v-if="!useServiceAccount" :label="t('llm.editor.customUrl')" optional :tip="t('llm.editor.customUrlHelp')">
        <NInput v-model:value="form.vertexUrl" placeholder="https://aiplatform.googleapis.com/v1" />
      </FormField>
      <FormField v-else :label="t('llm.editor.serviceAccount')" required :tip="t('llm.editor.serviceAccountHelp')" :help="serviceAccountStatus">
        <label class="inline-form">
          <input type="file" accept=".json,application/json" hidden @change="pickServiceAccount" />
          <NButton tag="span">{{ t('llm.editor.chooseFile') }}</NButton>
        </label>
      </FormField>
    </template>

    <FormField v-if="!useServiceAccount" label="API Key" required :tip="t('llm.editor.keyHelp')">
      <NInput
        v-model:value="form.key" type="password" show-password-on="click" class="secret-field" :input-props="{ autocomplete: 'new-password' }"
        :placeholder="savedKey ? t('llm.editor.keySaved', { key: savedKey.key_masked || '' }) : t('llm.editor.keyPlaceholder')"
      />
    </FormField>

    <FormField :label="t('llm.editor.model')" required :tip="t('llm.editor.modelHelp')">
      <NSelect v-model:value="form.model" filterable tag :options="modelOptions" :loading="fetching" :placeholder="t('llm.editor.modelPlaceholder')" @update:show="onModelMenu" @update:value="onModelPicked">
        <template #empty>{{ connectionReady ? t('llm.editor.modelEmpty') : t('llm.editor.modelTypeHint') }}</template>
        <template #action>
          <div class="model-action">
            <span :class="{ 'bad-text': models.error }" :title="models.error">{{ modelsStatus }}</span>
            <NButton v-if="connectionReady" size="tiny" quaternary :disabled="fetching" @click="fetchModels()">
              <template #icon><AppIcon name="refresh" /></template>{{ fetching ? t('llm.editor.loadingModels') : t('llm.editor.refresh') }}
            </NButton>
          </div>
        </template>
      </NSelect>
    </FormField>

    <div class="field-row">
      <FormField :label="t('llm.editor.concurrency')" :tip="t('llm.editor.concurrencyHelp')">
        <NInputNumber v-model:value="form.concurrency" :min="1" :max="8" :precision="0" />
      </FormField>
      <FormField :label="t('llm.editor.reasoning')" :tip="t('llm.editor.reasoningHelp')">
        <NSelect v-model:value="form.effort" :options="effortOptions" />
      </FormField>
    </div>
    <SwitchRow v-model="form.enabled" :label="t('llm.accepting')" :help="t('llm.editor.acceptingHelp')" />

    <template #footer>
      <div class="modal-actions">
        <NButton v-if="editing" type="error" secondary @click="remove">{{ t('llm.editor.delete') }}</NButton>
        <span class="spacer" />
        <NButton @click="close">{{ t('common.cancel') }}</NButton>
        <NButton type="primary" :loading="saving" @click="save">{{ t('common.save') }}</NButton>
      </div>
    </template>
  </NModal>
</template>

<style>
.model-action{display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:11.5px;color:var(--text-3)}
.model-action>span{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
</style>
