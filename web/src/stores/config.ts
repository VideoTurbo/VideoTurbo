// 主控配置（翻译节点、运行设置、部署信息）。只在打开相关页面和保存后读取。
import { shallowRef } from 'vue'
import { api, type AppConfig } from '@/api'

export const config = shallowRef<AppConfig | null>(null)

export async function loadConfig(): Promise<AppConfig> {
  config.value = await api.config()
  return config.value
}
