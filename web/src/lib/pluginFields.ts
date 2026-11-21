// 插件表单（每份配置、插件设置）的取值：打开表单时的初始值，提交时的值。
import type { FieldValue, PluginField } from '@/api'

/** 表单里的值：文本类是字符串，数字是 number 或 null，开关是 boolean。 */
export type FieldValues = Record<string, FieldValue>

/** 打开表单时的值：密钥字段留空（已保存的不回显），其余用已保存的值，没有时用默认值。 */
export function initialValues(fields: PluginField[], saved: Record<string, FieldValue> = {}): FieldValues {
  return Object.fromEntries(fields.map((field) => {
    const value = field.secret ? undefined : saved[field.key] ?? field.default
    if (field.type === 'switch') return [field.key, value === true]
    if (field.type === 'number') return [field.key, typeof value === 'number' ? value : null]
    return [field.key, value == null ? '' : String(value)]
  }))
}

function conditionText(value: FieldValue | undefined): string {
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  return value == null ? '' : String(value)
}

/** 按其他字段当前的值（没填的取默认值）判断字段是否显示，见字段的 visible_when。 */
export function isVisible(field: PluginField, fields: PluginField[], values: FieldValues): boolean {
  if (!field.visible_when) return true
  return Object.entries(field.visible_when).every(([key, allowed]) => {
    const value = values[key] ?? fields.find((item) => item.key === key)?.default
    return allowed.includes(conditionText(value))
  })
}

/** 当前显示的字段。 */
export function visibleFields(fields: PluginField[], values: FieldValues): PluginField[] {
  return fields.filter((field) => isVisible(field, fields, values))
}

/** 提交的值：不显示的字段不提交（主程序会清掉它的值）；密钥字段只在填写了时提交（留空表示不修改）；
 *  其他字段都提交，空值表示清空。 */
export function payloadValues(fields: PluginField[], values: FieldValues): FieldValues {
  const result: FieldValues = {}
  for (const field of visibleFields(fields, values)) {
    const value = values[field.key]
    if (field.type === 'switch') result[field.key] = value === true
    else if (field.type === 'number') result[field.key] = typeof value === 'number' ? value : null
    else {
      const text = value == null ? '' : String(value)
      const trimmed = field.trim === false ? text : text.trim()
      if (!field.secret || trimmed) result[field.key] = trimmed
    }
  }
  return result
}

/** 已保存过的密钥字段：控制台只拿到掩码，表单里显示“已保存，留空保持不变”。 */
export function savedSecrets(fields: PluginField[], saved: Record<string, FieldValue> = {}): Set<string> {
  return new Set(fields.filter((field) => field.secret && !!saved[field.key]).map((field) => field.key))
}
