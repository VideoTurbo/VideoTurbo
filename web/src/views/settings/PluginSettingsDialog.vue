<script setup lang="ts">
// 插件设置弹窗：插件卡片的设置按钮、添加媒体库时选择还没设置的网盘插件都打开它。
// 插件设置被这个插件的所有配置共用；网盘插件只有插件设置。保存后插件之前保存的登录状态会清空（设置有变化时）。
import { NButton, NModal } from 'naive-ui'
import { computed, ref, watch } from 'vue'
import { api, errorMessage, type PluginSettingsInfo } from '@/api'
import PluginFields from '@/components/PluginFields.vue'
import { toast } from '@/lib/feedback'
import { hostOf } from '@/lib/format'
import { initialValues, payloadValues, savedSecrets, type FieldValues } from '@/lib/pluginFields'
import { t } from '@/i18n'

const props = defineProps<{ pluginId: string | null }>()
const show = defineModel<boolean>({ required: true })
const emit = defineEmits<{ saved: [] }>()

const info = ref<PluginSettingsInfo | null>(null)
const values = ref<FieldValues>({})
const loadError = ref('')
const result = ref<{ ok: boolean; text: string } | null>(null)
const busy = ref(false)
const saved = computed(() => savedSecrets(info.value?.fields || [], info.value?.values))

watch(show, async (open) => {
  if (!open || !props.pluginId) return
  info.value = null
  loadError.value = ''
  result.value = null
  try {
    info.value = await api.pluginSettings(props.pluginId)
    values.value = initialValues(info.value.fields, info.value.values)
  } catch (error) {
    loadError.value = errorMessage(error)
  }
}, { immediate: true })

async function test(): Promise<void> {
  if (!info.value) return
  busy.value = true
  result.value = { ok: true, text: t('compute.providers.testing') }
  try {
    const body = await api.testPluginSettings(info.value.id, payloadValues(info.value.fields, values.value))
    result.value = { ok: true, text: body.message }
  } catch (error) {
    result.value = { ok: false, text: errorMessage(error) }
  } finally {
    busy.value = false
  }
}

async function save(): Promise<void> {
  if (!info.value) return
  busy.value = true
  try {
    await api.savePluginSettings(info.value.id, payloadValues(info.value.fields, values.value))
    show.value = false
    toast.ok(t('plugins.settings.saved', { label: info.value.label }))
    emit('saved')
  } catch (error) {
    result.value = { ok: false, text: t('compute.providers.saveFailed', { error: errorMessage(error) }) }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <NModal v-model:show="show" preset="card" class="modal-card" :title="info ? t('plugins.settings.titleLabel', { label: info.label }) : t('plugins.settings.title')">
    <div v-if="loadError" class="cloud-result bad">{{ t('plugins.settings.loadFailed', { error: loadError }) }}</div>
    <template v-else-if="info">
      <div class="plugin-settings-head">
        <span class="plugin-icon"><img v-if="info.icon" :src="info.icon" alt=""><span v-else>{{ info.label.slice(0, 1).toUpperCase() }}</span></span>
        <span class="source-option-text">
          <b>{{ info.label }}</b>
          <span>{{ info.description || (info.testable ? t('plugins.settings.sourceLead') : t('plugins.settings.sharedLead')) }}<template v-if="info.homepage"> · <a :href="info.homepage" target="_blank" rel="noopener noreferrer">{{ hostOf(info.homepage) }}</a></template></span>
        </span>
      </div>
      <PluginFields v-model="values" :fields="info.fields" :saved="saved" />
      <div v-if="result" class="cloud-result" :class="result.ok ? 'good' : 'bad'">{{ result.text }}</div>
    </template>
    <template #footer>
      <div class="modal-actions">
        <span class="spacer" />
        <NButton @click="show = false">{{ t('common.cancel') }}</NButton>
        <NButton v-if="info?.testable" :disabled="busy" @click="test">{{ t('plugins.settings.test') }}</NButton>
        <NButton type="primary" :disabled="busy || !info" @click="save">{{ t('common.save') }}</NButton>
      </div>
    </template>
  </NModal>
</template>
