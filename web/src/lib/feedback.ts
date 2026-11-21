// 全局提示与确认框。用 Naive UI 的脱离上下文 API，任何模块都能直接调用。
import { createDiscreteApi, type MessageReactive, type MessageType } from 'naive-ui'
import { computed, h } from 'vue'
import { errorMessage } from '@/api/http'
import { naiveOverrides, naiveTheme } from '@/theme'
import { t } from '@/i18n'

const { message, dialog } = createDiscreteApi(['message', 'dialog'], {
  configProviderProps: computed(() => ({ theme: naiveTheme.value, themeOverrides: naiveOverrides.value })),
  // 提示出现在顶栏下方居中，不遮挡顶栏按钮；每条都可以手动关闭。
  messageProviderProps: { containerStyle: { top: '60px' }, placement: 'top', duration: 3200, max: 4, keepAliveOnHover: true, closable: true },
})

export const toast = {
  info: (text: string) => void message.info(text),
  ok: (text: string) => void message.success(text),
  fail: (text: string) => void message.error(text, { duration: 5000 }),
  /** “xx失败：原因” */
  error: (prefix: string, error: unknown) => void message.error(t('common.withError', { prefix, error: errorMessage(error) }), { duration: 5000 }),
  /** 单行提醒：标题超长省略，悬停看全文；extra 是标题后不被省略的补充（如“另有 2 条”）。点击执行 onClick 并关闭这条提示。 */
  notice: (type: MessageType, text: string, onClick: () => void, extra = '') => {
    let instance: MessageReactive | null = null
    const content = () => h('span', {
      class: 'toast-notice',
      title: text,
      onClick: () => {
        onClick()
        instance?.destroy()
      },
    }, [h('span', { class: 'toast-notice-text' }, text), extra ? h('span', { class: 'toast-notice-extra' }, extra) : null])
    instance = message.create(content, { type, duration: type === 'error' ? 5000 : 3200 })
  },
}

export interface ConfirmOptions {
  title?: string
  confirmLabel?: string
  danger?: boolean
}

/** 弹出确认框；确认返回 true，取消、关闭、按 Esc 或点遮罩返回 false。 */
export function confirm(text: string, options: ConfirmOptions = {}): Promise<boolean> {
  return new Promise((resolve) => {
    dialog.create({
      type: options.danger ? 'error' : 'info',
      title: options.title ?? t('common.confirmTitle'),
      content: text,
      // 正文里的换行照常显示；长地址和哈希可以在任意位置折行。
      contentStyle: { whiteSpace: 'pre-line', overflowWrap: 'anywhere' },
      positiveText: options.confirmLabel ?? t('common.confirm'),
      negativeText: t('common.cancel'),
      autoFocus: false,
      onPositiveClick: () => resolve(true),
      // 其余关闭途径最终都会触发 onAfterLeave；Promise 只会被第一次 resolve 决定。
      onAfterLeave: () => resolve(false),
    })
  })
}
