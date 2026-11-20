<script setup lang="ts">
// 按插件声明的字段渲染表单：单行文本（密钥为密码框）、多行文本、数字、下拉框、开关。
// 每份配置（媒体服务器、通知、云平台）和插件设置共用。
import { NInput, NInputNumber, NSelect } from 'naive-ui'
import type { PluginField } from '@/api'
import type { FieldValues } from '@/lib/pluginFields'
import FormField from './FormField.vue'
import SwitchRow from './SwitchRow.vue'
import { t } from '@/i18n'

defineProps<{
  fields: PluginField[]
  /** 已保存过的密钥字段：留空表示不修改。 */
  saved?: Set<string>
}>()
const values = defineModel<FieldValues>({ required: true })

function text(key: string): string {
  const value = values.value[key]
  return value == null ? '' : String(value)
}
function number(key: string): number | null {
  const value = values.value[key]
  return typeof value === 'number' ? value : null
}
function set(key: string, value: string | number | boolean | null): void {
  values.value = { ...values.value, [key]: value }
}
</script>

<template>
  <template v-for="field in fields" :key="field.key">
    <SwitchRow v-if="field.type === 'switch'" :model-value="values[field.key] === true" :label="field.label" :tip="field.help" @update:model-value="set(field.key, $event)" />
    <FormField v-else :label="field.label" :tip="field.help" :required="field.required !== false" :optional="field.required === false && field.type !== 'select'">
      <NInputNumber
        v-if="field.type === 'number'"
        :value="number(field.key)" :min="field.min ?? undefined" :max="field.max ?? undefined"
        :precision="field.integer === false ? undefined : 0" :placeholder="field.placeholder || ''" clearable
        @update:value="set(field.key, $event)"
      />
      <NSelect
        v-else-if="field.type === 'select'"
        :value="text(field.key) || null" :options="field.options || []" :placeholder="field.placeholder || t('plugins.fields.select')"
        :clearable="!field.required" @update:value="set(field.key, $event ?? '')"
      />
      <NInput
        v-else-if="field.type === 'textarea'"
        :value="text(field.key)" type="textarea" :autosize="{ minRows: 3, maxRows: 10 }"
        :input-props="{ autocomplete: 'off', spellcheck: false }"
        :placeholder="field.secret && saved?.has(field.key) ? t('plugins.fields.savedKeep') : field.placeholder || ''"
        @update:value="set(field.key, $event)"
      />
      <NInput
        v-else
        :value="text(field.key)"
        :type="field.secret ? 'password' : 'text'" :show-password-on="field.secret ? 'click' : undefined"
        :input-props="{ autocomplete: field.secret ? 'new-password' : 'off' }"
        :placeholder="field.secret && saved?.has(field.key) ? t('plugins.fields.savedKeep') : field.placeholder || ''"
        @update:value="set(field.key, $event)"
      />
    </FormField>
  </template>
</template>
