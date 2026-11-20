import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

// 开发时把接口转发到本机主控；主控地址可用 VIDEO_TURBO_SERVER 覆盖。
const server = process.env.VIDEO_TURBO_SERVER || 'http://127.0.0.1:8765'

export default defineConfig({
  plugins: [vue()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  build: {
    // 产物直接输出到 Python 包内，由主控作为静态文件提供。
    outDir: '../src/video_turbo/orchestrator/static/dashboard',
    emptyOutDir: true,
    // 控制台在局域网内使用，单个约 240 KB（gzip）的脚本无需再拆分。
    chunkSizeWarningLimit: 1000,
  },
  server: {
    proxy: Object.fromEntries(
      ['/api', '/login', '/favicon.svg'].map((path) => [path, server]),
    ),
  },
  // 测试固定用简体中文（jsdom 的浏览器语言是英文）。
  test: { environment: 'jsdom', include: ['tests/**/*.test.ts'], setupFiles: ['tests/setup.ts'] },
})
