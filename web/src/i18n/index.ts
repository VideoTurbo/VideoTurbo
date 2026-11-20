// 控制台多语言。文案在 zh-Hans.ts（源语言）和 en.ts 中，键按“区域.含义”命名，{name} 是参数。
// 语言在页面加载时确定：先看本浏览器保存的选择，再看浏览器语言；切换后刷新页面生效。
// 接口请求带上 Accept-Language，后端返回的提示（报错、通知、任务失败原因）也按同一语言显示。
import { dateEnUS, dateZhCN, enUS, zhCN } from 'naive-ui'
import en from './en'
import zhHans from './zh-Hans'

export type Locale = 'zh-Hans' | 'en'
export type MessageKey = keyof typeof zhHans
export type Messages = Record<MessageKey, string>
type Params = Record<string, string | number | null | undefined>

export const LOCALES: { id: Locale; label: string }[] = [
  { id: 'zh-Hans', label: '简体中文' },
  { id: 'en', label: 'English' },
]
const catalogs: Record<Locale, Messages> = { 'zh-Hans': zhHans, en }
const STORAGE_KEY = 'video-turbo-locale'

export function normalizeLocale(value: string | null | undefined): Locale | null {
  const code = (value || '').trim().toLowerCase()
  if (code.startsWith('zh')) return 'zh-Hans'
  if (code.startsWith('en')) return 'en'
  return null
}

function detect(): Locale {
  try {
    const saved = normalizeLocale(localStorage.getItem(STORAGE_KEY))
    if (saved) return saved
  } catch {
    // 隐私模式等情况下读不到本地存储，按浏览器语言。
  }
  const languages = typeof navigator === 'undefined' ? [] : [...(navigator.languages || []), navigator.language]
  for (const language of languages) {
    const found = normalizeLocale(language)
    if (found) return found
  }
  // 浏览器语言既不是中文也不是英文时显示英文。
  return languages.length ? 'en' : 'zh-Hans'
}

export const locale: Locale = detect()
export const naiveLocale = locale === 'en' ? enUS : zhCN
export const naiveDateLocale = locale === 'en' ? dateEnUS : dateZhCN
if (typeof document !== 'undefined') document.documentElement.lang = locale === 'en' ? 'en' : 'zh-CN'

export function t(key: MessageKey, params?: Params): string {
  const template = catalogs[locale][key] ?? zhHans[key] ?? key
  if (!params) return template
  return template.replace(/\{(\w+)\}/g, (match, name: string) => (name in params ? String(params[name] ?? '') : match))
}

export function setLocale(next: Locale): void {
  try {
    localStorage.setItem(STORAGE_KEY, next)
  } catch {
    // 保存失败时只影响下次打开，本次仍然切换。
  }
  location.reload()
}
