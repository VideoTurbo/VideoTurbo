import { describe, expect, it } from 'vitest'
import type { PluginField } from '@/api/types'
import { initialValues, payloadValues, savedSecrets, visibleFields } from '@/lib/pluginFields'

const fields: PluginField[] = [
  { key: 'url', label: '地址', type: 'text', secret: false, default: 'https://a' },
  { key: 'token', label: '令牌', type: 'text', secret: true },
  { key: 'cookie', label: 'Cookie', type: 'textarea', secret: true, trim: false },
  { key: 'port', label: '端口', type: 'number', default: 22 },
  { key: 'region', label: '区域', type: 'select', options: [{ value: 'cn', label: '国内' }] },
  { key: 'fast', label: '加速', type: 'switch', default: false },
]

describe('plugin fields', () => {
  it('fills saved values and defaults, never echoes secrets', () => {
    expect(initialValues(fields, { token: '••••••', port: 2222, fast: true })).toEqual({
      url: 'https://a', token: '', cookie: '', port: 2222, region: '', fast: true,
    })
    expect(initialValues(fields)).toMatchObject({ port: 22, fast: false })
    expect([...savedSecrets(fields, { token: '••••••', cookie: '' })]).toEqual(['token'])
  })

  it('submits every non-secret value and only filled secrets', () => {
    expect(payloadValues(fields, { url: ' https://b ', token: '  ', cookie: ' a=1 ', port: null, region: '', fast: true })).toEqual({
      url: 'https://b', cookie: ' a=1 ', port: null, region: '', fast: true,
    })
  })

  it('shows and submits only the fields whose condition matches', () => {
    const conditional: PluginField[] = [
      { key: 'auth', label: '登录方式', type: 'select', default: 'token', options: [] },
      { key: 'token', label: '令牌', type: 'text', secret: true, visible_when: { auth: ['token'] } },
      { key: 'username', label: '用户名', type: 'text', secret: false, visible_when: { auth: ['password'] } },
      { key: 'proxy', label: '代理', type: 'switch' },
      { key: 'proxy_url', label: '代理地址', type: 'text', secret: false, visible_when: { proxy: ['true'] } },
    ]
    const keys = (values: Record<string, string | boolean>) => visibleFields(conditional, values).map((field) => field.key)
    expect(keys({})).toEqual(['auth', 'token', 'proxy'])
    expect(keys({ auth: 'password', proxy: true })).toEqual(['auth', 'username', 'proxy', 'proxy_url'])
    expect(keys({ auth: 'guest', proxy: false })).toEqual(['auth', 'proxy'])
    expect(payloadValues(conditional, { auth: 'guest', token: 'x', username: 'u', proxy: false, proxy_url: 'p' })).toEqual({
      auth: 'guest', proxy: false,
    })
  })
})
