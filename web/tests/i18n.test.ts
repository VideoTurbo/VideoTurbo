import { describe, expect, it } from 'vitest'
import en from '@/i18n/en'
import zhHans from '@/i18n/zh-Hans'
import { normalizeLocale, t } from '@/i18n'

const placeholders = (text: string) => [...text.matchAll(/\{(\w+)\}/g)].map((match) => match[1]).sort()

describe('i18n', () => {
  it('has the same keys and placeholders in every language', () => {
    expect(Object.keys(en).sort()).toEqual(Object.keys(zhHans).sort())
    for (const [key, text] of Object.entries(zhHans)) {
      expect(placeholders(en[key as keyof typeof en]), key).toEqual(placeholders(text))
      expect(en[key as keyof typeof en].trim(), key).not.toBe('')
    }
  })

  it('fills placeholders and normalizes browser languages', () => {
    expect(t('common.selectedCount', { count: 3 })).toBe('已选 3 项')
    expect(normalizeLocale('zh-TW')).toBe('zh-Hans')
    expect(normalizeLocale('en-GB')).toBe('en')
    expect(normalizeLocale('fr')).toBeNull()
  })
})
