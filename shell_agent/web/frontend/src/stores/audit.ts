import { defineStore } from 'pinia'

import { errorMessage, http } from '../api/http'
import type { AuditRecord } from '../api/protocol'

export const useAuditStore = defineStore('audit', {
  state: () => ({
    records: [] as AuditRecord[],
    target: '',
    loading: false,
    error: '',
  }),
  actions: {
    async load() {
      this.loading = true
      this.error = ''
      try {
        const data = await http.get<{ records: AuditRecord[] }>('/api/audit', {
          target: this.target,
          limit: 100,
        })
        this.records = data.records ?? []
      } catch (error) {
        this.error = errorMessage(error)
      } finally {
        this.loading = false
      }
    },
  },
})
