import { defineStore } from 'pinia'

import { errorMessage, http } from '../api/http'
import type { AuditRecord } from '../api/protocol'

interface PaginationPayload {
  page: number
  page_size: number
  total: number
  total_pages: number
}

export const useAuditStore = defineStore('audit', {
  state: () => ({
    records: [] as AuditRecord[],
    target: '',
    page: 1,
    pageSize: 20,
    total: 0,
    totalPages: 1,
    loading: false,
    error: '',
  }),
  actions: {
    async load() {
      this.loading = true
      this.error = ''
      try {
        const data = await http.get<{ records: AuditRecord[]; pagination?: PaginationPayload }>('/api/audit', {
          target: this.target,
          page: this.page,
          page_size: this.pageSize,
        })
        this.records = data.records ?? []
        if (data.pagination) {
          this.page = data.pagination.page
          this.pageSize = data.pagination.page_size
          this.total = data.pagination.total
          this.totalPages = data.pagination.total_pages
        }
      } catch (error) {
        this.error = errorMessage(error)
      } finally {
        this.loading = false
      }
    },
    async search() {
      this.page = 1
      await this.load()
    },
    async goToPage(page: number) {
      this.page = Math.max(1, Math.min(page, this.totalPages))
      await this.load()
    },
  },
})
