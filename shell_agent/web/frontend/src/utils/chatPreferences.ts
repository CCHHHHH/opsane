import type { ConfirmMode } from '../api/protocol'

export const DEFAULT_CHAT_CONFIRM_MODE: ConfirmMode = 'auto_safe'
export const CHAT_CONFIRM_MODE_STORAGE_KEY = 'shell-agent:chat:confirm-mode:v1'

const CONFIRM_MODES: readonly ConfirmMode[] = [
  'interactive',
  'auto_safe',
  'dry_run',
  'full_access',
]

function getStorage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage
  } catch {
    return null
  }
}

function isConfirmMode(value: unknown): value is ConfirmMode {
  return typeof value === 'string' && CONFIRM_MODES.includes(value as ConfirmMode)
}

export function loadChatConfirmMode(): ConfirmMode {
  const storage = getStorage()
  if (!storage) return DEFAULT_CHAT_CONFIRM_MODE
  try {
    const stored = storage.getItem(CHAT_CONFIRM_MODE_STORAGE_KEY)
    if (isConfirmMode(stored)) return stored
    if (stored !== null) storage.removeItem(CHAT_CONFIRM_MODE_STORAGE_KEY)
  } catch {
    // Storage may be unavailable or contain malformed data. Keep the safe default.
  }
  return DEFAULT_CHAT_CONFIRM_MODE
}

export function saveChatConfirmMode(mode: ConfirmMode): void {
  try {
    getStorage()?.setItem(CHAT_CONFIRM_MODE_STORAGE_KEY, mode)
  } catch {
    // Preference persistence must not block chat operation.
  }
}
