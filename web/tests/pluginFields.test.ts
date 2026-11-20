import { describe, expect, it } from 'vitest'
import type { PluginField } from '@/api/types'
import { initialValues, payloadValues, savedSecrets } from '@/lib/pluginFields'

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
})
