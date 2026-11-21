// 桌面版外壳。控制台在桌面版窗口里打开时，外壳只开放少数几个命令（主题、窗口按钮），
// 见 desktop/apps/videoturbo/capabilities/console.json；在普通浏览器里这些都不存在。

interface ShellBridge {
  invoke(cmd: string, args?: Record<string, unknown>): Promise<unknown>
}

declare global {
  interface Window {
    __TAURI_INTERNALS__?: ShellBridge
    /** 桌面版外壳在页面加载前写入它记住的主题，换了端口、本地存储为空时沿用。 */
    __VIDEO_TURBO_SHELL_THEME__?: string
    /** 桌面版外壳在页面加载前写入的标题栏样式，index.html 把它标到 <html data-titlebar> 上。 */
    __VIDEO_TURBO_TITLEBAR__?: string
  }
}

/**
 * 外壳的标题栏样式（index.html 已标到 <html data-titlebar> 上，样式表据此让位）：
 * - overlay：macOS 透明标题栏，窗口按钮在左上角，顶栏要让位；
 * - custom：Windows 没有系统标题栏，顶栏右侧由页面画窗口按钮；
 * - 空：普通浏览器。
 */
export const titlebar = window.__VIDEO_TURBO_TITLEBAR__ ?? ''

/** 调用外壳命令；不在桌面版里或外壳不支持时返回 undefined，不抛错。 */
export async function shellInvoke<T = unknown>(cmd: string, args?: Record<string, unknown>): Promise<T | undefined> {
  try {
    return (await window.__TAURI_INTERNALS__?.invoke(cmd, args)) as T | undefined
  } catch {
    return undefined
  }
}
