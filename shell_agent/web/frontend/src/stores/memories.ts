import { defineStore } from 'pinia'

import { ApiError, errorMessage, http } from '../api/http'
import type { MemoryRecord, ProfileCandidateRecord } from '../api/protocol'
import { useNotificationsStore } from './notifications'

export interface MemoryInput {
  subject: string
  predicate: string
  value: string
  target: string
  type?: string
  status?: string
  confidence?: number
  expires_at?: string
  evidence_summary?: string
}

interface PaginationPayload {
  page: number
  page_size: number
  total: number
  total_pages: number
}

interface PaginationState {
  page: number
  pageSize: number
  total: number
  totalPages: number
}

type KnowledgeSection = 'memories' | 'candidates' | 'issues'

function paginationState(): PaginationState {
  return { page: 1, pageSize: 20, total: 0, totalPages: 1 }
}

function applyPagination(state: PaginationState, payload?: PaginationPayload) {
  if (!payload) return
  state.page = payload.page
  state.pageSize = payload.page_size
  state.total = payload.total
  state.totalPages = payload.total_pages
}

export const useMemoriesStore = defineStore('memories', {
  state: () => ({
    items: [] as MemoryRecord[],
    candidates: [] as ProfileCandidateRecord[],
    expiredCandidates: [] as ProfileCandidateRecord[],
    issues: [] as MemoryRecord[],
    query: '',
    typeFilter: '',
    statusFilter: '',
    memoryPagination: paginationState(),
    candidatePagination: paginationState(),
    issuePagination: paginationState(),
    loading: false,
    saving: false,
    error: '',
  }),
  actions: {
    async load(query?: string) {
      const activeQuery = query ?? this.query
      this.query = activeQuery
      this.loading = true
      this.error = ''
      try {
        const [memoryData, candidateData, expiredCandidateData, issueData] = await Promise.all([
          http.get<{ memories: MemoryRecord[]; pagination?: PaginationPayload }>('/api/memories', {
            q: activeQuery,
            type: this.typeFilter,
            status: this.statusFilter,
            page: this.memoryPagination.page,
            page_size: this.memoryPagination.pageSize,
          }),
          http.get<{ candidates: ProfileCandidateRecord[]; pagination?: PaginationPayload }>('/api/service-profile-candidates', {
            status: 'pending',
            page: this.candidatePagination.page,
            page_size: this.candidatePagination.pageSize,
          }),
          http.get<{ candidates: ProfileCandidateRecord[] }>('/api/service-profile-candidates', {
            status: 'expired',
            limit: 50,
          }),
          http.get<{ issues: MemoryRecord[]; pagination?: PaginationPayload }>('/api/knowledge/conflicts', {
            page: this.issuePagination.page,
            page_size: this.issuePagination.pageSize,
          }),
        ])
        this.items = memoryData.memories ?? []
        this.candidates = candidateData.candidates ?? []
        this.expiredCandidates = expiredCandidateData.candidates ?? []
        this.issues = issueData.issues ?? []
        applyPagination(this.memoryPagination, memoryData.pagination)
        applyPagination(this.candidatePagination, candidateData.pagination)
        applyPagination(this.issuePagination, issueData.pagination)
      } catch (error) {
        this.error = errorMessage(error)
      } finally {
        this.loading = false
      }
    },
    async search() {
      this.memoryPagination.page = 1
      await this.load()
    },
    async goToPage(section: KnowledgeSection, page: number) {
      const pagination = section === 'memories'
        ? this.memoryPagination
        : section === 'candidates'
          ? this.candidatePagination
          : this.issuePagination
      pagination.page = Math.max(1, Math.min(page, pagination.totalPages))
      await this.load()
    },
    async create(input: MemoryInput) {
      const notifications = useNotificationsStore()
      this.saving = true
      try {
        await http.post<{ ok: boolean; memory: MemoryRecord }>('/api/memories', input)
        await this.load()
        notifications.success('记忆已创建')
      } catch (error) {
        notifications.error(errorMessage(error))
        throw error
      } finally {
        this.saving = false
      }
    },
    async update(id: string | number, input: Partial<MemoryInput>) {
      const notifications = useNotificationsStore()
      this.saving = true
      try {
        await http.put<{ ok: boolean; memory: MemoryRecord }>(`/api/memories/${encodeURIComponent(String(id))}`, input)
        await this.load()
        notifications.success('记忆已更新')
      } catch (error) {
        notifications.error(errorMessage(error))
        throw error
      } finally {
        this.saving = false
      }
    },
    async remove(id: string | number) {
      const notifications = useNotificationsStore()
      try {
        await http.delete<{ ok: boolean }>(`/api/memories/${encodeURIComponent(String(id))}`)
        await this.load()
        notifications.success('记忆已删除')
      } catch (error) {
        notifications.error(errorMessage(error))
        throw error
      }
    },
    async acceptCandidate(candidate: ProfileCandidateRecord, changes?: Record<string, unknown>) {
      const notifications = useNotificationsStore()
      this.saving = true
      try {
        const beforeRevision = Number(candidate.before_snapshot?.revision ?? 0)
        await http.post<{ ok: boolean }>(
          `/api/service-profile-candidates/${encodeURIComponent(candidate.id)}/accept`,
          { proposed_changes: changes ?? candidate.proposed_changes, expected_revision: beforeRevision },
        )
        await this.load()
        notifications.success('服务画像候选已写入')
        return true
      } catch (error) {
        if (error instanceof ApiError && error.status === 409) {
          await this.load()
          notifications.show(errorMessage(error), 'info', 4800)
          return false
        }
        notifications.error(errorMessage(error))
        throw error
      } finally {
        this.saving = false
      }
    },
    async rejectCandidate(candidate: ProfileCandidateRecord) {
      const notifications = useNotificationsStore()
      try {
        await http.post<{ ok: boolean }>(
          `/api/service-profile-candidates/${encodeURIComponent(candidate.id)}/reject`,
          {},
        )
        await this.load()
        notifications.success('服务画像候选已忽略')
      } catch (error) {
        notifications.error(errorMessage(error))
        throw error
      }
    },
    async rebaseCandidate(candidate: ProfileCandidateRecord) {
      const notifications = useNotificationsStore()
      this.saving = true
      try {
        await http.post<{ ok: boolean; candidate: ProfileCandidateRecord | null }>(
          `/api/service-profile-candidates/${encodeURIComponent(candidate.id)}/rebase`,
          {},
        )
        await this.load()
        notifications.success('候选已基于最新画像重新合并，请重新审核')
        return true
      } catch (error) {
        await this.load()
        notifications.error(errorMessage(error))
        return false
      } finally {
        this.saving = false
      }
    },
  },
})
