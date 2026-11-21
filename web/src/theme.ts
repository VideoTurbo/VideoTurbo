// 主题配色。布局样式读 styles/base.css 里的 CSS 变量，
// Naive UI 组件读这里的 themeOverrides，两边的色值保持一致。
import { darkTheme, lightTheme, type GlobalThemeOverrides } from 'naive-ui'
import { computed, ref, watchEffect } from 'vue'

export type ThemeName = 'dark' | 'light'
const STORAGE_KEY = 'video-turbo-theme'

interface Palette {
  bg: string; bg1: string; panel: string; panel2: string; panel3: string; float: string
  line: string; line2: string; line3: string; hover: string
  text: string; text2: string; text3: string
  accent: string; accentHover: string; accentPressed: string; primaryText: string
  green: string; yellow: string; red: string; blue: string
  overlay: string
}

const palettes: Record<ThemeName, Palette> = {
  dark: {
    bg: '#09090b', bg1: '#0e0e11', panel: '#121215', panel2: '#17171b', panel3: '#1f1f25', float: '#18181d',
    line: 'rgba(255,255,255,.07)', line2: 'rgba(255,255,255,.1)', line3: 'rgba(255,255,255,.16)', hover: 'rgba(255,255,255,.035)',
    text: '#ededf0', text2: '#a1a1ab', text3: '#6b6b76',
    accent: '#8b93ff', accentHover: '#9da4ff', accentPressed: '#6d75f4', primaryText: '#0d0e22',
    green: '#4ade80', yellow: '#fbbf24', red: '#f87171', blue: '#60a5fa',
    overlay: 'rgba(3,3,5,.62)',
  },
  light: {
    bg: '#f5f6fa', bg1: '#f8f9fc', panel: '#fff', panel2: '#f1f3f8', panel3: '#e7eaf2', float: '#fff',
    line: 'rgba(30,41,59,.1)', line2: 'rgba(30,41,59,.15)', line3: 'rgba(30,41,59,.24)', hover: 'rgba(30,41,59,.04)',
    text: '#202431', text2: '#505b6e', text3: '#657086',
    accent: '#515bd4', accentHover: '#454fc5', accentPressed: '#3943b5', primaryText: '#fff',
    green: '#15803d', yellow: '#946200', red: '#c62d3e', blue: '#2563c4',
    overlay: 'rgba(30,41,59,.3)',
  },
}

const SANS = 'Inter,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",system-ui,sans-serif'
const MONO = 'ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace'

function overrides(p: Palette): GlobalThemeOverrides {
  return {
    common: {
      fontFamily: SANS, fontFamilyMono: MONO, fontSize: '13px', fontSizeSmall: '12px', fontSizeMedium: '13px', fontSizeLarge: '14px',
      heightSmall: '26px', heightMedium: '32px', heightLarge: '36px',
      borderRadius: '8px', borderRadiusSmall: '6px',
      bodyColor: p.bg, cardColor: p.panel, modalColor: p.panel, popoverColor: p.float, tableColor: p.panel,
      inputColor: p.bg1, inputColorDisabled: p.panel2, actionColor: p.bg1,
      hoverColor: p.hover, pressedColor: p.panel3, buttonColor2: p.panel2, buttonColor2Hover: p.panel3, buttonColor2Pressed: p.panel3,
      borderColor: p.line2, dividerColor: p.line,
      textColorBase: p.text, textColor1: p.text, textColor2: p.text, textColor3: p.text2,
      textColorDisabled: p.text3, placeholderColor: p.text3, iconColor: p.text3,
      primaryColor: p.accent, primaryColorHover: p.accentHover, primaryColorPressed: p.accentPressed, primaryColorSuppl: p.accent,
      successColor: p.green, successColorHover: p.green, successColorPressed: p.green, successColorSuppl: p.green,
      warningColor: p.yellow, warningColorHover: p.yellow, warningColorPressed: p.yellow, warningColorSuppl: p.yellow,
      errorColor: p.red, errorColorHover: p.red, errorColorPressed: p.red, errorColorSuppl: p.red,
      infoColor: p.blue, infoColorHover: p.blue, infoColorPressed: p.blue, infoColorSuppl: p.blue,
    },
    Button: {
      fontWeight: '500', color: p.panel2, colorHover: p.panel3, colorPressed: p.panel3, colorFocus: p.panel3,
      textColorHover: p.text, textColorPressed: p.text, textColorFocus: p.text,
      border: `1px solid ${p.line2}`, borderHover: `1px solid ${p.line3}`, borderPressed: `1px solid ${p.line3}`, borderFocus: `1px solid ${p.line3}`,
      textColorPrimary: p.primaryText, textColorHoverPrimary: p.primaryText, textColorPressedPrimary: p.primaryText, textColorFocusPrimary: p.primaryText,
      fontWeightStrong: '600',
    },
    Modal: { color: p.panel },
    Card: { borderRadius: '18px', color: p.panel, colorModal: p.panel, borderColor: p.line3, titleFontSizeMedium: '16px', paddingMedium: '20px 24px' },
    Dialog: { borderRadius: '18px', color: p.panel, titleFontSize: '16px' },
    Drawer: { color: p.panel },
    Tooltip: { color: p.float, textColor: p.text, borderRadius: '8px' },
    Dropdown: { color: p.float, borderRadius: '10px', optionColorHover: p.panel3 },
    Select: { peers: { InternalSelectMenu: { color: p.float, borderRadius: '12px', optionColorPending: p.hover } } },
    Message: { color: p.float, colorInfo: p.float, colorSuccess: p.float, colorError: p.float, colorWarning: p.float, borderRadius: '10px' },
    Switch: { railColorActive: p.accentPressed },
  }
}

// ---- 主题模式：跟随系统（默认）/ 日间 / 夜间 ----
// 选择存在本浏览器里；桌面版还会告诉外壳，让窗口标题栏和外壳自己的页面用同一种深浅色。

export type ThemeMode = 'system' | ThemeName
const MODES: readonly ThemeMode[] = ['system', 'light', 'dark']

interface ShellBridge {
  invoke(cmd: string, args?: Record<string, unknown>): Promise<unknown>
}
declare global {
  interface Window {
    __TAURI_INTERNALS__?: ShellBridge
    /** 桌面版外壳在页面加载前写入它记住的主题，换了端口、本地存储为空时沿用。 */
    __VIDEO_TURBO_SHELL_THEME__?: string
  }
}

function isMode(value: unknown): value is ThemeMode {
  return MODES.includes(value as ThemeMode)
}

function storedMode(): ThemeMode {
  try {
    const value = localStorage.getItem(STORAGE_KEY)
    if (isMode(value)) return value
  } catch {
    // 存储不可用时按下面的默认值处理。
  }
  const shell = window.__VIDEO_TURBO_SHELL_THEME__
  return isMode(shell) ? shell : 'system'
}

const systemQuery = window.matchMedia('(prefers-color-scheme: light)')
const systemTheme = ref<ThemeName>(systemQuery.matches ? 'light' : 'dark')
systemQuery.addEventListener('change', (event) => {
  systemTheme.value = event.matches ? 'light' : 'dark'
})

export const themeMode = ref<ThemeMode>(storedMode())
export const themeName = computed<ThemeName>(() => (themeMode.value === 'system' ? systemTheme.value : themeMode.value))
export const naiveTheme = computed(() => (themeName.value === 'light' ? lightTheme : darkTheme))
export const naiveOverrides = computed(() => overrides(palettes[themeName.value]))

watchEffect(() => {
  document.documentElement.dataset.theme = themeName.value
})

/** 桌面版：让外壳的窗口跟着变。浏览器里没有外壳，什么也不做。 */
async function syncShell(mode: ThemeMode): Promise<void> {
  try {
    await window.__TAURI_INTERNALS__?.invoke('set_theme', { theme: mode })
  } catch {
    // 旧版外壳没有这个命令，忽略即可。
  }
  // 窗口强制深/浅色时，网页读到的“系统深浅色”也会跟着变；改回跟随系统后重新读一次。
  systemTheme.value = systemQuery.matches ? 'light' : 'dark'
}

export function setThemeMode(mode: ThemeMode): void {
  themeMode.value = mode
  try {
    localStorage.setItem(STORAGE_KEY, mode)
  } catch {
    // 存储不可用时仍可切换，只是刷新后不保留。
  }
  void syncShell(mode)
}

void syncShell(themeMode.value)
