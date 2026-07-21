import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { CHAT_DRAFTS_STORAGE_KEY, useChatDraftsStore } from './chatDrafts'

beforeEach(() => {
  window.localStorage.clear()
  setActivePinia(createPinia())
})

describe('chat drafts', () => {
  it('keeps independent drafts for each chat session', () => {
    const drafts = useChatDraftsStore()

    drafts.setDraft('session-a', '升级 MySQL')
    drafts.setDraft('session-b', '检查磁盘')

    expect(drafts.forSession('session-a')).toBe('升级 MySQL')
    expect(drafts.forSession('session-b')).toBe('检查磁盘')
    expect(drafts.forSession('session-c')).toBe('')
  })

  it('restores session drafts after the store is recreated', () => {
    useChatDraftsStore().setDraft('session-a', '尚未发送的内容')

    setActivePinia(createPinia())

    expect(useChatDraftsStore().forSession('session-a')).toBe('尚未发送的内容')
  })

  it('clears only the requested session draft', () => {
    const drafts = useChatDraftsStore()
    drafts.setDraft('session-a', 'draft a')
    drafts.setDraft('session-b', 'draft b')

    drafts.clearDraft('session-a')

    expect(drafts.forSession('session-a')).toBe('')
    expect(drafts.forSession('session-b')).toBe('draft b')
    expect(JSON.parse(window.localStorage.getItem(CHAT_DRAFTS_STORAGE_KEY) || '{}')).toEqual({
      'session-b': 'draft b',
    })
  })

  it('discards malformed persisted draft data', () => {
    window.localStorage.setItem(CHAT_DRAFTS_STORAGE_KEY, '{not-json')
    setActivePinia(createPinia())

    expect(useChatDraftsStore().forSession('session-a')).toBe('')
    expect(window.localStorage.getItem(CHAT_DRAFTS_STORAGE_KEY)).toBeNull()
  })
})
