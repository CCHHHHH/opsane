import { defineStore } from 'pinia'

import { errorMessage, http } from '../api/http'
import type { RuntimeState } from '../api/protocol'

export const useAppStore = defineStore('app', {
  state: () => ({
    stats: { executed: 0, failed: 0 },
    loading: false,
    error: '',
  }),
  actions: {
    async initialize() {
      this.loading = true
      this.error = ''
      try {
        const state = await http.get<RuntimeState>('/api/state')
        this.stats = state.stats
      } catch (error) {
        this.error = errorMessage(error)
      } finally {
        this.loading = false
      }
    },
  },
})
