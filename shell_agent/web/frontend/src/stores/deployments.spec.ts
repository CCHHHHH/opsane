import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  confirmDeploymentPlan,
  confirmDeploymentRollback,
  createDeploymentRun,
  getDeploymentRun,
  listDeploymentRuns,
  type DeploymentRunRecord,
} from '../api/deployments'
import { useDeploymentsStore } from './deployments'

vi.mock('../api/deployments', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/deployments')>()
  return {
    ...actual,
    createDeploymentRun: vi.fn(),
    getDeploymentRun: vi.fn(),
    listDeploymentRuns: vi.fn(),
    confirmDeploymentPlan: vi.fn(),
    cancelDeploymentRun: vi.fn(),
    confirmDeploymentRollback: vi.fn(),
  }
})

function run(status: DeploymentRunRecord['status'] = 'waiting_plan_confirm'): DeploymentRunRecord {
  return {
    id: 'deprun-1', session_id: 'session-1', service_id: 'camel', target: 'dev-01',
    environment: 'dev', status, plan_hash: 'a'.repeat(64), error: '', result_summary: '',
    plan: {
      service: { service_id: 'camel', service_name: 'Camel', target: 'dev-01', environment: 'dev' },
      artifact: { file_id: 'file-1', name: 'camel.jar', size: 1024, sha256: 'b'.repeat(64) },
      steps: [],
    },
    steps: [], events: [], updated_at: '2026-07-16T10:00:00Z',
  }
}

describe('deployments store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    vi.clearAllMocks()
  })

  it('restores the active durable run from the server after a page refresh', async () => {
    const active = run('staging_upload')
    vi.mocked(listDeploymentRuns).mockResolvedValue([active])
    vi.mocked(getDeploymentRun).mockResolvedValue(active)
    const store = useDeploymentsStore()

    await store.loadSession('session-1')

    expect(listDeploymentRuns).toHaveBeenCalledWith('session-1')
    expect(getDeploymentRun).toHaveBeenCalledWith('deprun-1')
    expect(store.run?.status).toBe('staging_upload')
    expect(store.locksComposer).toBe(true)
  })

  it('prefers an active server run over a remembered completed run from another tab', async () => {
    localStorage.setItem('shell-agent.deployment-run.session-1', 'old-completed-run')
    const active = { ...run('starting'), id: 'new-active-run' }
    vi.mocked(listDeploymentRuns).mockResolvedValue([run('completed'), active])
    vi.mocked(getDeploymentRun).mockResolvedValue(active)
    const store = useDeploymentsStore()

    await store.loadSession('session-1')

    expect(store.run?.id).toBe('new-active-run')
    expect(store.locksComposer).toBe(true)
  })

  it('submits an immutable plan hash once and blocks a duplicate confirmation immediately', async () => {
    const store = useDeploymentsStore()
    store.sessionId = 'session-1'
    store.run = run()
    let resolve!: (value: DeploymentRunRecord) => void
    vi.mocked(confirmDeploymentPlan).mockReturnValue(new Promise((done) => { resolve = done }))

    const first = store.confirmPlan()
    const second = store.confirmPlan()

    expect(store.pendingAction).toBe('confirm')
    expect(confirmDeploymentPlan).toHaveBeenCalledTimes(1)
    expect(confirmDeploymentPlan).toHaveBeenCalledWith('deprun-1', 'a'.repeat(64))
    resolve(run('confirmed'))
    await Promise.all([first, second])
    expect(store.run?.status).toBe('confirmed')
    expect(store.pendingAction).toBe('')
  })

  it('reuses the request id after an ambiguous create failure', async () => {
    const store = useDeploymentsStore()
    const input = { session_id: 'session-1', service_id: 'camel', file_id: 'file-1' }
    vi.mocked(createDeploymentRun)
      .mockRejectedValueOnce(new Error('connection closed'))
      .mockResolvedValueOnce(run())

    await expect(store.create(input)).rejects.toThrow('connection closed')
    const firstRequestId = vi.mocked(createDeploymentRun).mock.calls[0][0].request_id
    await store.create(input)

    expect(createDeploymentRun).toHaveBeenCalledTimes(2)
    expect(vi.mocked(createDeploymentRun).mock.calls[1][0].request_id).toBe(firstRequestId)
    expect(store.pendingCreateRequestId).toBe('')
  })

  it.each([
    ['staging_upload', true],
    ['rollback_required', true],
    ['unknown', true],
    ['manual_intervention', true],
    ['completed', false],
    ['rolled_back', false],
    ['precheck_failed', false],
  ] as const)('sets composer lock for %s to %s', (status, locked) => {
    const store = useDeploymentsStore()
    store.run = run(status)
    expect(store.locksComposer).toBe(locked)
  })

  it('confirms rollback once and keeps the run locked while rollback starts', async () => {
    const store = useDeploymentsStore()
    store.sessionId = 'session-1'
    store.run = run('rollback_required')
    vi.mocked(confirmDeploymentRollback).mockResolvedValue(run('rollback_confirmed'))

    await store.confirmRollback()

    expect(confirmDeploymentRollback).toHaveBeenCalledTimes(1)
    expect(store.run?.status).toBe('rollback_confirmed')
    expect(store.locksComposer).toBe(true)
  })

  it('keeps unknown runs locked without polling forever', () => {
    const store = useDeploymentsStore()
    store.run = run('unknown')

    expect(store.locksComposer).toBe(true)
    expect(store.isPolling).toBe(false)
  })
})
