// 运行设置：/api/config 各分区的表单定义，以及表单值与接口值之间的转换。
import type { ConfigSection, ConfigValues } from '@/api/types'
import { t } from '@/i18n'

interface FieldBase {
  key: string; label: string; help?: string
  /** 同一分区里某个开关的 key：开关关闭时隐藏本字段（值仍照常保存）。 */
  visibleWhen?: string
}
export type ConfigField = FieldBase & (
  | { type: 'switch' }
  | { type: 'int'; min: number; max: number }
  | { type: 'time' } // 每日 HH:mm，接口里是 cron 表达式
  | { type: 'select'; options: Record<string, string> }
  | { type: 'text'; placeholder?: string }
)

export const configSchemas: Record<ConfigSection, ConfigField[]> = {
  scan: [
    { key: 'enabled', type: 'switch', label: t('settings.form.scanEnabled'), help: t('settings.form.scanEnabledHelp') },
    { key: 'schedule', type: 'time', label: t('settings.form.scanTime'), visibleWhen: 'enabled' },
    { key: 'settle_minutes', type: 'int', min: 0, max: 10080, label: t('settings.form.settle'),
      help: t('settings.form.settleHelp') },
  ],
  subtitles: [
    { key: 'skip_existing_target', type: 'switch', label: t('settings.form.skipExisting'),
      help: t('settings.form.skipExistingHelp') },
    { key: 'accept_other_chinese_variant', type: 'switch', label: t('settings.form.chineseVariants'),
      help: t('settings.form.chineseVariantsHelp') },
    { key: 'unlabeled_embedded_subtitle_policy', type: 'select', label: t('settings.form.unlabeled'), options: { ignore: t('settings.form.unlabeled.check'), skip: t('settings.form.unlabeled.skip') },
      help: t('settings.form.unlabeledHelp') },
  ],
  translation: [
    { key: 'enabled', type: 'switch', label: t('settings.form.translationEnabled') },
    { key: 'job_concurrency', type: 'int', min: 1, max: 4, label: t('settings.form.jobConcurrency'), help: t('settings.form.jobConcurrencyHelp') },
  ],
  server: [
    { key: 'log_level', type: 'select', label: t('settings.form.logLevel'), options: { DEBUG: t('settings.form.log.debug'), INFO: t('settings.form.log.info'), WARNING: t('settings.form.log.warning'), ERROR: t('settings.form.log.error') } },
  ],
}

/** 表单里的值：开关是布尔，整数是数字或空，其余是字符串。 */
export type FormValue = boolean | number | null | string
export type FormValues = Record<string, FormValue>

const pad = (n: number | string) => String(n).padStart(2, '0')

/** cron “分 时 * * *” → “HH:mm”；不是每日定时的表达式显示为 03:00。 */
function cronToTime(cron: unknown): string {
  const parts = String(cron || '').split(/\s+/)
  const daily = parts.length === 5 && /^\d+$/.test(parts[0]!) && /^\d+$/.test(parts[1]!)
  return daily ? `${pad(parts[1]!)}:${pad(parts[0]!)}` : '03:00'
}
function timeToCron(time: string): string {
  const [hour, minute] = (time || '03:00').split(':').map(Number)
  return `${minute} ${hour} * * *`
}

export function toFormValues(section: ConfigSection, saved: ConfigValues = {}): FormValues {
  return Object.fromEntries(configSchemas[section].map((field): [string, FormValue] => {
    const value = saved[field.key]
    switch (field.type) {
      case 'switch': return [field.key, Boolean(value)]
      case 'int': return [field.key, typeof value === 'number' ? value : null]
      case 'time': return [field.key, cronToTime(value)]
      default: return [field.key, value == null ? '' : String(value)]
    }
  }))
}

/** 表单值 → 接口值；有问题时抛出给用户看的错误。 */
export function toConfigPayload(section: ConfigSection, values: FormValues): ConfigValues {
  const payload: ConfigValues = {}
  for (const field of configSchemas[section]) {
    const value = values[field.key]
    switch (field.type) {
      case 'int':
        if (value === null || value === '') throw new Error(t('settings.form.required', { label: field.label }))
        if (!Number.isInteger(value)) throw new Error(t('settings.form.integer', { label: field.label }))
        if ((value as number) < field.min || (value as number) > field.max) throw new Error(t('settings.form.range', { label: field.label, min: field.min, max: field.max }))
        payload[field.key] = value
        break
      case 'time': payload[field.key] = timeToCron(String(value)); break
      case 'text': payload[field.key] = String(value ?? '').trim(); break
      default: payload[field.key] = value
    }
  }
  return payload
}
