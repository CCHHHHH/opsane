import { beforeEach, describe, expect, it } from 'vitest'

import { applyTheme, nextTheme, normalizeTheme, readStoredTheme, THEME_META_COLORS, THEME_STORAGE_KEY } from './theme'

describe('theme preferences', () => {
  beforeEach(() => {
    document.documentElement.removeAttribute('data-theme')
    document.documentElement.removeAttribute('style')
    document.head.innerHTML = '<meta name="theme-color" content="#000000">'
    localStorage.clear()
  })

  it('defaults invalid or missing values to the current dark theme', () => {
    expect(normalizeTheme(undefined)).toBe('dark')
    expect(normalizeTheme('system')).toBe('dark')
    expect(readStoredTheme()).toBe('dark')
  })

  it('toggles between dark and light', () => {
    expect(nextTheme('dark')).toBe('light')
    expect(nextTheme('light')).toBe('dark')
  })

  it('applies and persists a light theme across reloads', () => {
    applyTheme('light')

    expect(document.documentElement.dataset.theme).toBe('light')
    expect(document.documentElement.style.colorScheme).toBe('light')
    expect(document.querySelector('meta[name="theme-color"]')?.getAttribute('content')).toBe(THEME_META_COLORS.light)
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('light')
    expect(readStoredTheme()).toBe('light')
  })
})
