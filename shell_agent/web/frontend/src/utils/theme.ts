export type ThemeMode = 'dark' | 'light'

export const THEME_STORAGE_KEY = 'opsane-theme'

export const THEME_META_COLORS: Record<ThemeMode, string> = {
  dark: '#07111d',
  light: '#f4f8fc',
}

type ThemeStorage = Pick<Storage, 'getItem' | 'setItem'>

function browserStorage(): ThemeStorage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage
  } catch {
    return null
  }
}

export function normalizeTheme(value: unknown): ThemeMode {
  return value === 'light' ? 'light' : 'dark'
}

export function readStoredTheme(storage: ThemeStorage | null = browserStorage()): ThemeMode {
  try {
    return normalizeTheme(storage?.getItem(THEME_STORAGE_KEY))
  } catch {
    return 'dark'
  }
}

export function nextTheme(theme: ThemeMode): ThemeMode {
  return theme === 'dark' ? 'light' : 'dark'
}

export function applyTheme(theme: ThemeMode, storage: ThemeStorage | null = browserStorage()): ThemeMode {
  const normalized = normalizeTheme(theme)
  if (typeof document !== 'undefined') {
    document.documentElement.dataset.theme = normalized
    document.documentElement.style.colorScheme = normalized
    document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')?.setAttribute('content', THEME_META_COLORS[normalized])
  }
  try {
    storage?.setItem(THEME_STORAGE_KEY, normalized)
  } catch {
    // Theme changes still work when browser storage is unavailable.
  }
  return normalized
}
