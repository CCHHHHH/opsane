import { defineStore } from 'pinia'

export const CHAT_DRAFTS_STORAGE_KEY = 'opsane:chat:drafts:v1'

type ChatDrafts = Record<string, string>

function getStorage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage
  } catch {
    return null
  }
}

function loadDrafts(): ChatDrafts {
  const storage = getStorage()
  if (!storage) return {}
  try {
    const raw = storage.getItem(CHAT_DRAFTS_STORAGE_KEY)
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      storage.removeItem(CHAT_DRAFTS_STORAGE_KEY)
      return {}
    }
    return Object.fromEntries(
      Object.entries(parsed).filter(([sessionId, draft]) => sessionId && typeof draft === 'string' && draft.length > 0),
    )
  } catch {
    try {
      storage.removeItem(CHAT_DRAFTS_STORAGE_KEY)
    } catch {
      // Draft persistence must never block the composer.
    }
    return {}
  }
}

function persistDrafts(drafts: ChatDrafts): void {
  const storage = getStorage()
  if (!storage) return
  try {
    if (Object.keys(drafts).length) {
      storage.setItem(CHAT_DRAFTS_STORAGE_KEY, JSON.stringify(drafts))
    } else {
      storage.removeItem(CHAT_DRAFTS_STORAGE_KEY)
    }
  } catch {
    // Keep the in-memory draft usable when browser storage is unavailable.
  }
}

export const useChatDraftsStore = defineStore('chat-drafts', {
  state: () => ({
    drafts: loadDrafts() as ChatDrafts,
  }),
  getters: {
    forSession: (state) => (sessionId: string): string => (
      sessionId ? state.drafts[sessionId] ?? '' : ''
    ),
  },
  actions: {
    setDraft(sessionId: string, draft: string) {
      if (!sessionId) return
      const next = { ...this.drafts }
      if (draft.length) next[sessionId] = draft
      else delete next[sessionId]
      this.drafts = next
      persistDrafts(next)
    },
    clearDraft(sessionId: string) {
      if (!sessionId || !(sessionId in this.drafts)) return
      const next = { ...this.drafts }
      delete next[sessionId]
      this.drafts = next
      persistDrafts(next)
    },
  },
})
