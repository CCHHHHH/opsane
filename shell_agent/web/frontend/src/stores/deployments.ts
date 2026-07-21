import { defineStore } from 'pinia'

import {
  cancelDeploymentRun,
  confirmDeploymentPlan,
  confirmDeploymentRollback,
  createDeploymentRun,
  getDeploymentRun,
  listDeploymentRuns,
  type CreateDeploymentRunInput,
  type DeploymentRunRecord,
  type DeploymentRunStatus,
} from '../api/deployments'
import { ApiError, errorMessage } from '../api/http'

const STORAGE_PREFIX = 'shell-agent.deployment-run.'
const POLL_INTERVAL_MS = 900

let pollTimer: ReturnType<typeof setTimeout> | null = null
let pollGeneration = 0

const SAFE_TERMINAL_STATUSES = new Set<DeploymentRunStatus>([
  'completed',
  'precheck_failed',
  'plan_rejected',
  'lock_conflict',
  'step_failed',
  'rolled_back',
  'canceled',
])

export function deploymentRunNeedsAttention(status: DeploymentRunStatus): boolean {
  return !SAFE_TERMINAL_STATUSES.has(status)
}

export function deploymentRunIsTerminal(status: DeploymentRunStatus): boolean {
  return SAFE_TERMINAL_STATUSES.has(status)
    || status === 'manual_intervention'
    || status === 'unknown'
}

function storageKey(sessionId: string): string {
  return `${STORAGE_PREFIX}${sessionId}`
}

function rememberedRunId(sessionId: string): string {
  try {
    return globalThis.localStorage?.getItem(storageKey(sessionId)) ?? ''
  } catch {
    return ''
  }
}

function rememberRun(sessionId: string, runId: string) {
  try {
    if (runId) globalThis.localStorage?.setItem(storageKey(sessionId), runId)
    else globalThis.localStorage?.removeItem(storageKey(sessionId))
  } catch {
    // Persistence is a recovery aid. A blocked localStorage must not block a run.
  }
}

function requestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `deploy-${crypto.randomUUID()}`
  }
  return `deploy-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

export const useDeploymentsStore = defineStore('deployments', {
  state: () => ({
    sessionId: '',
    run: null as DeploymentRunRecord | null,
    loading: false,
    pendingAction: '' as '' | 'create' | 'confirm' | 'cancel' | 'rollback',
    pendingCreateSignature: '',
    pendingCreateRequestId: '',
    error: '',
  }),
  getters: {
    locksComposer: (state): boolean => Boolean(
      state.run && deploymentRunNeedsAttention(state.run.status),
    ),
    isPolling: (state): boolean => Boolean(
      state.run && !deploymentRunIsTerminal(state.run.status),
    ),
  },
  actions: {
    applyRun(run: DeploymentRunRecord) {
      if (this.sessionId && run.session_id && run.session_id !== this.sessionId) return false
      this.run = run
      const sessionId = run.session_id || this.sessionId
      if (sessionId) rememberRun(sessionId, run.id)
      this.error = ''
      return true
    },
    async loadSession(sessionId: string) {
      this.stopPolling()
      this.sessionId = sessionId
      this.run = null
      this.pendingAction = ''
      this.error = ''
      this.loading = true
      try {
        const runs = await listDeploymentRuns(sessionId)
        const run = [...runs].sort((left, right) => {
          const attention = Number(deploymentRunNeedsAttention(right.status))
            - Number(deploymentRunNeedsAttention(left.status))
          if (attention) return attention
          return String(right.updated_at || right.created_at || '')
            .localeCompare(String(left.updated_at || left.created_at || ''))
        })[0] ?? null
        if (!run) return null
        // The list endpoint selects the durable run; fetch its detail before
        // rendering so refresh never resets persisted step progress to the
        // frozen plan's default pending state, even briefly.
        const detailed = await getDeploymentRun(run.id)
        if (this.sessionId === sessionId) this.applyRun(detailed)
        return detailed
      } catch (error) {
        const runId = rememberedRunId(sessionId)
        if (runId) {
          try {
            const run = await getDeploymentRun(runId)
            if (this.sessionId === sessionId) this.applyRun(run)
            return run
          } catch (fallbackError) {
            error = fallbackError
          }
        }
        if (this.sessionId === sessionId) {
          if (error instanceof ApiError && error.status === 404) rememberRun(sessionId, '')
          else this.error = errorMessage(error)
        }
        return null
      } finally {
        if (this.sessionId === sessionId) this.loading = false
      }
    },
    async adoptRun(sessionId: string, runId: string) {
      if (!sessionId || !runId) return null
      this.sessionId = sessionId
      rememberRun(sessionId, runId)
      return this.refresh(runId)
    },
    async refresh(runId?: string) {
      const resolvedRunId = runId || this.run?.id || rememberedRunId(this.sessionId)
      if (!resolvedRunId || !this.sessionId) return null
      const expectedSession = this.sessionId
      try {
        const run = await getDeploymentRun(resolvedRunId)
        if (this.sessionId === expectedSession) this.applyRun(run)
        return run
      } catch (error) {
        if (this.sessionId === expectedSession) this.error = errorMessage(error)
        return null
      }
    },
    async create(input: CreateDeploymentRunInput) {
      if (this.pendingAction || !input.session_id) return this.run
      this.sessionId = input.session_id
      this.pendingAction = 'create'
      this.error = ''
      const signature = `${input.session_id}:${input.service_id}:${input.file_id}`
      if (this.pendingCreateSignature !== signature || !this.pendingCreateRequestId) {
        this.pendingCreateSignature = signature
        this.pendingCreateRequestId = input.request_id || requestId()
      }
      try {
        const run = await createDeploymentRun({
          ...input,
          request_id: this.pendingCreateRequestId,
        })
        if (this.sessionId === input.session_id) this.applyRun(run)
        if (this.pendingCreateSignature === signature) {
          this.pendingCreateSignature = ''
          this.pendingCreateRequestId = ''
        }
        return run
      } catch (error) {
        if (this.sessionId === input.session_id) this.error = errorMessage(error)
        throw error
      } finally {
        if (this.pendingAction === 'create') this.pendingAction = ''
      }
    },
    async confirmPlan() {
      const run = this.run
      if (!run || run.status !== 'waiting_plan_confirm' || !run.plan_hash || this.pendingAction) return run
      this.pendingAction = 'confirm'
      this.error = ''
      try {
        const updated = await confirmDeploymentPlan(run.id, run.plan_hash)
        if (this.run?.id === run.id) this.applyRun(updated)
        return updated
      } catch (error) {
        if (this.run?.id === run.id) this.error = errorMessage(error)
        throw error
      } finally {
        if (this.pendingAction === 'confirm') this.pendingAction = ''
      }
    },
    async cancel() {
      const run = this.run
      if (!run || this.pendingAction) return run
      this.pendingAction = 'cancel'
      this.error = ''
      try {
        const updated = await cancelDeploymentRun(run.id)
        if (this.run?.id === run.id) this.applyRun(updated)
        return updated
      } catch (error) {
        if (this.run?.id === run.id) this.error = errorMessage(error)
        throw error
      } finally {
        if (this.pendingAction === 'cancel') this.pendingAction = ''
      }
    },
    async confirmRollback() {
      const run = this.run
      if (!run || run.status !== 'rollback_required' || this.pendingAction) return run
      this.pendingAction = 'rollback'
      this.error = ''
      try {
        const updated = await confirmDeploymentRollback(run.id)
        if (this.run?.id === run.id) this.applyRun(updated)
        return updated
      } catch (error) {
        if (this.run?.id === run.id) this.error = errorMessage(error)
        throw error
      } finally {
        if (this.pendingAction === 'rollback') this.pendingAction = ''
      }
    },
    startPolling(sessionId?: string) {
      const resolvedSessionId = sessionId || this.sessionId
      this.stopPolling()
      if (!resolvedSessionId) return
      const generation = pollGeneration
      const tick = async () => {
        if (generation !== pollGeneration || this.sessionId !== resolvedSessionId) return
        if (this.run && !deploymentRunIsTerminal(this.run.status)) await this.refresh(this.run.id)
        if (generation !== pollGeneration || this.sessionId !== resolvedSessionId) return
        if (this.run && !deploymentRunIsTerminal(this.run.status)) {
          pollTimer = setTimeout(tick, POLL_INTERVAL_MS)
        }
      }
      if (this.run && !deploymentRunIsTerminal(this.run.status)) {
        pollTimer = setTimeout(tick, POLL_INTERVAL_MS)
      }
    },
    stopPolling() {
      pollGeneration += 1
      if (pollTimer) clearTimeout(pollTimer)
      pollTimer = null
    },
    reset() {
      this.stopPolling()
      this.sessionId = ''
      this.run = null
      this.loading = false
      this.pendingAction = ''
      this.pendingCreateSignature = ''
      this.pendingCreateRequestId = ''
      this.error = ''
    },
  },
})
