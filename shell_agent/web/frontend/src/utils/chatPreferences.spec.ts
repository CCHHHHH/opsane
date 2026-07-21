import { afterEach, describe, expect, it } from 'vitest'

import {
  CHAT_CONFIRM_MODE_STORAGE_KEY,
  DEFAULT_CHAT_CONFIRM_MODE,
  loadChatConfirmMode,
  saveChatConfirmMode,
} from './chatPreferences'

afterEach(() => {
  window.localStorage.clear()
})

describe('chat preferences', () => {
  it('uses auto safe when no preference has been saved', () => {
    expect(loadChatConfirmMode()).toBe(DEFAULT_CHAT_CONFIRM_MODE)
  })

  it.each(['interactive', 'auto_safe', 'dry_run', 'full_access'] as const)(
    'restores the saved %s confirmation mode',
    (mode) => {
      saveChatConfirmMode(mode)
      expect(loadChatConfirmMode()).toBe(mode)
    },
  )

  it('discards an unsupported stored value', () => {
    window.localStorage.setItem(CHAT_CONFIRM_MODE_STORAGE_KEY, 'unrestricted')

    expect(loadChatConfirmMode()).toBe(DEFAULT_CHAT_CONFIRM_MODE)
    expect(window.localStorage.getItem(CHAT_CONFIRM_MODE_STORAGE_KEY)).toBeNull()
  })
})
