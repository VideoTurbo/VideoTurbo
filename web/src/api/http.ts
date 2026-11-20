// 所有接口请求的唯一出口：统一处理 JSON、错误信息、登录过期和界面语言。
import { locale, t } from '@/i18n'

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
  }
}

interface ValidationIssue {
  msg?: string
}

function errorDetail(body: unknown, status: number): string {
  const detail = (body as { detail?: string | ValidationIssue[] } | null)?.detail
  if (Array.isArray(detail)) return detail.map((item) => item.msg || String(item)).join(t('common.listJoin'))
  return detail || `HTTP ${status}`
}

export async function request<T>(url: string, method = 'GET', body?: unknown): Promise<T> {
  const headers: Record<string, string> = { 'Accept-Language': locale }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const response = await fetch(url, {
    method,
    cache: 'no-store',
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  // 会话过期或未登录：除登录接口本身外，一律回到登录页。
  if (response.status === 401 && !url.includes('/api/auth/')) location.href = '/login'
  const data: unknown = await response.json().catch(() => null)
  if (!response.ok) throw new ApiError(errorDetail(data, response.status), response.status)
  return data as T
}

export const get = <T>(url: string) => request<T>(url)
export const post = <T>(url: string, body?: unknown) => request<T>(url, 'POST', body)
export const put = <T>(url: string, body?: unknown) => request<T>(url, 'PUT', body)
export const del = <T>(url: string) => request<T>(url, 'DELETE')

/** 路径片段转义。 */
export const seg = (value: string | number) => encodeURIComponent(String(value))

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
