import { defineStore } from 'pinia'

import { errorMessage, http } from '../api/http'
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

export const useMemoriesStore = defineStore('memories', {
  state: () => ({
    items: [] as MemoryRecord[],
    candidates: [] as ProfileCandidateRecord[],
    issues: [] as MemoryRecord[],
    query: '',
    typeFilter: '',
    statusFilter: '',
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
        const [memoryData, candidateData, issueData] = await Promise.all([
          http.get<{ memories: MemoryRecord[] }>('/api/memories', {
            q: activeQuery,
            type: this.typeFilter,
            status: this.statusFilter,
          }),
          http.get<{ candidates: ProfileCandidateRecord[] }>('/api/service-profile-candidates', { status: 'pending' }),
          http.get<{ issues: MemoryRecord[] }>('/api/knowledge/conflicts'),
        ])
        this.items = memoryData.memories ?? []
        this.candidates = candidateData.candidates ?? []
        this.issues = issueData.issues ?? []
      } catch (error) {
        this.error = errorMessage(error)
      } finally {
        this.loading = false
      }
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
      } catch (error) {
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
  },
})
