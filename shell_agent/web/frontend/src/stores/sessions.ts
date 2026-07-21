import { defineStore } from 'pinia'

import { errorMessage, http } from '../api/http'
import type { SessionDetail, SessionSummary, SessionType, SessionUpdatedEvent } from '../api/protocol'

export const useSessionsStore = defineStore('sessions', {
  state: () => ({
    items: [] as SessionSummary[],
    selected: null as SessionDetail | null,
    loading: false,
    error: '',
  }),
  actions: {
    async load(type?: SessionType, query = '') {
      this.loading = true
      this.error = ''
      try {
        const data = await http.get<{ sessions: SessionSummary[] }>('/api/sessions', { type, q: query })
        this.items = data.sessions ?? []
      } catch (error) {
        this.error = errorMessage(error)
      } finally {
        this.loading = false
      }
    },
    async create(type: SessionType, title = '') {
      const data = await http.post<{ session: SessionSummary }>('/api/sessions', { type, title })
      this.items.unshift(data.session)
      await this.select(data.session.id)
      return data.session
    },
    async select(id: string, messageLimit = 100) {
      const data = await http.get<{ session: SessionDetail }>(`/api/sessions/${encodeURIComponent(id)}`, {
        message_limit: messageLimit,
      })
      this.selected = data.session
      return data.session
    },
    async rename(id: string, title: string) {
      const data = await http.patch<{ session: SessionSummary }>(`/api/sessions/${encodeURIComponent(id)}`, { title })
      const index = this.items.findIndex((item) => item.id === id)
      if (index >= 0) this.items[index] = data.session
      if (this.selected?.id === id) this.selected.title = data.session.title
    },
    applyRealtimeUpdate(event: SessionUpdatedEvent) {
      if (!event.session_id || !event.title) return
      const index = this.items.findIndex((item) => item.id === event.session_id)
      if (index >= 0) {
        this.items[index] = { ...this.items[index], title: event.title }
      }
      if (this.selected?.id === event.session_id) {
        this.selected.title = event.title
      }
    },
    async pin(id: string, pinned: boolean) {
      const data = await http.put<{ session: SessionSummary }>(`/api/sessions/${encodeURIComponent(id)}/pin`, { pinned })
      const index = this.items.findIndex((item) => item.id === id)
      if (index >= 0) this.items[index] = data.session
      if (this.selected?.id === id) this.selected.pinned_at = data.session.pinned_at
    },
    async remove(id: string) {
      await http.delete<{ ok: boolean }>(`/api/sessions/${encodeURIComponent(id)}`)
      this.items = this.items.filter((item) => item.id !== id)
      if (this.selected?.id === id) this.selected = null
    },
  },
})
