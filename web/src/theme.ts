// 主题配色。布局样式读 styles/base.css 里的 CSS 变量，
// Naive UI 组件读这里的 themeOverrides，两边的色值保持一致。
import { darkTheme, lightTheme, type GlobalThemeOverrides } from 'naive-ui'
import { computed, ref } from 'vue'

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

function stored(): ThemeName {
  return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark'
}

export const themeName = ref<ThemeName>(stored())
export const naiveTheme = computed(() => (themeName.value === 'light' ? lightTheme : darkTheme))
export const naiveOverrides = computed(() => overrides(palettes[themeName.value]))

export function toggleTheme(): void {
  themeName.value = themeName.value === 'light' ? 'dark' : 'light'
  document.documentElement.dataset.theme = themeName.value
  try {
    localStorage.setItem(STORAGE_KEY, themeName.value)
  } catch {
    // 存储不可用时仍可切换，只是刷新后不保留。
  }
}
