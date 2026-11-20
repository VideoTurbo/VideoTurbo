<script setup lang="ts">
// 按 lib/settings.ts 的字段定义渲染一个配置分区的表单。
import { NInput, NInputNumber, NSelect, NTimePicker } from 'naive-ui'
import FormField from '@/components/FormField.vue'
import SwitchRow from '@/components/SwitchRow.vue'
import { computed } from 'vue'
import type { ConfigField, FormValues } from '@/lib/settings'

const props = defineProps<{ fields: ConfigField[] }>()
const values = defineModel<FormValues>({ required: true })
/** 依赖的开关关闭时隐藏字段。 */
const visible = computed(() => props.fields.filter((field) => !field.visibleWhen || values.value[field.visibleWhen] === true))

const options = (map: Record<string, string>) => Object.entries(map).map(([value, label]) => ({ value, label }))
</script>

<template>
  <template v-for="field in visible" :key="field.key">
    <SwitchRow v-if="field.type === 'switch'" v-model="values[field.key] as boolean" :label="field.label" :help="field.help" />
    <FormField v-else :label="field.label" :help="field.help">
      <NInputNumber v-if="field.type === 'int'" v-model:value="values[field.key] as number | null" :min="field.min" :max="field.max" :precision="0" />
      <NTimePicker v-else-if="field.type === 'time'" v-model:formatted-value="values[field.key] as string" format="HH:mm" value-format="HH:mm" :clearable="false" style="width: 140px" />
      <NSelect v-else-if="field.type === 'select'" v-model:value="values[field.key] as string" :options="options(field.options)" />
      <NInput v-else v-model:value="values[field.key] as string" :placeholder="field.placeholder" />
    </FormField>
  </template>
</template>
