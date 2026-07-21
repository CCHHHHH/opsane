import { http } from './http'

export type DeploymentRunStatus =
  | 'created'
  | 'precheck_running'
  | 'waiting_plan_confirm'
  | 'confirmed'
  | 'lock_acquiring'
  | 'locked'
  | 'staging_upload'
  | 'artifact_verified'
  | 'backup_running'
  | 'stopping'
  | 'switching'
  | 'starting'
  | 'postcheck_running'
  | 'succeeded'
  | 'finalizing'
  | 'completed'
  | 'precheck_failed'
  | 'plan_rejected'
  | 'lock_conflict'
  | 'step_failed'
  | 'rollback_required'
  | 'rollback_confirmed'
  | 'rollback_running'
  | 'rollback_postcheck'
  | 'rolled_back'
  | 'rollback_failed'
  | 'manual_intervention'
  | 'canceled'
  | 'unknown'

export type DeploymentStepStatus = 'pending' | 'running' | 'success' | 'failed' | 'skipped' | 'unknown'

export interface DeploymentServiceSnapshot {
  service_id?: string
  service_name?: string
  environment?: string
  target?: string
  deploy_dir?: string
  artifact_path?: string
  revision?: number
  [key: string]: unknown
}

export interface DeploymentArtifactSnapshot {
  file_id?: string
  name?: string
  size?: number
  sha256?: string
  [key: string]: unknown
}

export interface DeploymentPlanStep {
  id: string
  name: string
  phase: string
  action: string
  risk_level: string
  mutates_live?: boolean
  arguments?: Record<string, unknown>
}

export interface DeploymentRunStep extends DeploymentPlanStep {
  step_id: string
  step_index: number
  status: DeploymentStepStatus
  attempt?: number
  exit_code?: number | null
  stdout?: string
  stderr?: string
  error?: string
  started_at?: string | null
  completed_at?: string | null
}

export interface DeploymentPlanRecord {
  runbook_id?: string
  runbook_version?: string
  run_id?: string
  service: DeploymentServiceSnapshot
  artifact: DeploymentArtifactSnapshot
  steps: DeploymentPlanStep[]
}

export interface DeploymentRunRecord {
  id: string
  request_id?: string
  session_id: string
  service_id?: string
  target?: string
  environment?: string
  runbook_id?: string
  runbook_version?: string
  status: DeploymentRunStatus
  plan_hash: string
  confirmed_plan_hash?: string | null
  mutation_started?: boolean
  error?: string
  result_summary?: string
  created_at?: string
  updated_at?: string
  completed_at?: string | null
  plan: DeploymentPlanRecord
  steps: DeploymentRunStep[]
  events: Array<Record<string, unknown>>
}

export interface CreateDeploymentRunInput {
  session_id: string
  service_id: string
  file_id: string
  request_id?: string
}

type UnknownRecord = Record<string, unknown>

function record(value: unknown): UnknownRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as UnknownRecord : {}
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

function number(value: unknown, fallback = 0): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

function normalizePlanStep(value: unknown, index: number): DeploymentPlanStep {
  const step = record(value)
  return {
    id: text(step.id) || text(step.step_id) || `step-${index + 1}`,
    name: text(step.name) || `步骤 ${index + 1}`,
    phase: text(step.phase, 'execute'),
    action: text(step.action),
    risk_level: text(step.risk_level, 'safe'),
    mutates_live: Boolean(step.mutates_live),
    arguments: record(step.arguments),
  }
}

function normalizeStep(value: unknown, index: number, definition?: DeploymentPlanStep): DeploymentRunStep {
  const step = record(value)
  const base = definition ?? normalizePlanStep(step, index)
  return {
    ...base,
    id: text(step.id) || base.id,
    step_id: text(step.step_id) || base.id,
    step_index: number(step.step_index, index),
    status: text(step.status, 'pending') as DeploymentStepStatus,
    attempt: number(step.attempt, 0),
    exit_code: step.exit_code == null ? null : number(step.exit_code),
    stdout: text(step.stdout),
    stderr: text(step.stderr),
    error: text(step.error),
    started_at: text(step.started_at) || null,
    completed_at: text(step.completed_at) || null,
  }
}

/**
 * Keeps the UI tolerant of a direct record or a `{ run, steps, events }`
 * response. Backend contract changes stay isolated in this module.
 */
export function normalizeDeploymentRun(payload: unknown): DeploymentRunRecord {
  const envelope = record(payload)
  const run = record(envelope.run ?? envelope.deployment_run ?? payload)
  const planSource = record(run.plan_json ?? run.plan)
  const planSteps = Array.isArray(planSource.steps)
    ? planSource.steps.map(normalizePlanStep)
    : []
  const persistedSteps = Array.isArray(envelope.steps)
    ? envelope.steps
    : Array.isArray(run.steps) ? run.steps : []
  const stepById = new Map(planSteps.map((step) => [step.id, step]))
  const steps = persistedSteps.length
    ? persistedSteps.map((item, index) => {
        const raw = record(item)
        const id = text(raw.step_id) || text(raw.id)
        return normalizeStep(item, index, stepById.get(id))
      })
    : planSteps.map((step, index) => normalizeStep(step, index, step))
  const service = record(planSource.service ?? run.profile_snapshot)
  const artifact = record(planSource.artifact ?? run.artifact_snapshot)
  const id = text(run.id) || text(run.run_id) || text(planSource.run_id)
  if (!id) throw new Error('部署任务响应缺少 run id')

  return {
    id,
    request_id: text(run.request_id),
    session_id: text(run.session_id),
    service_id: text(run.service_id) || text(service.service_id),
    target: text(run.target) || text(service.target),
    environment: text(run.environment) || text(service.environment),
    runbook_id: text(run.runbook_id) || text(planSource.runbook_id),
    runbook_version: text(run.runbook_version) || text(planSource.runbook_version),
    status: text(run.status, 'unknown') as DeploymentRunStatus,
    plan_hash: text(run.plan_hash),
    confirmed_plan_hash: text(run.confirmed_plan_hash) || null,
    mutation_started: Boolean(run.mutation_started),
    error: text(run.error),
    result_summary: text(run.result_summary),
    created_at: text(run.created_at),
    updated_at: text(run.updated_at),
    completed_at: text(run.completed_at) || null,
    plan: {
      runbook_id: text(planSource.runbook_id),
      runbook_version: text(planSource.runbook_version),
      run_id: text(planSource.run_id) || id,
      service,
      artifact,
      steps: planSteps,
    },
    steps,
    events: Array.isArray(envelope.events)
      ? envelope.events.map(record)
      : Array.isArray(run.events) ? run.events.map(record) : [],
  }
}

export function createDeploymentRun(input: CreateDeploymentRunInput) {
  return http.post<unknown>('/api/deployment-runs', input).then(normalizeDeploymentRun)
}

export function getDeploymentRun(runId: string) {
  return http.get<unknown>(`/api/deployment-runs/${encodeURIComponent(runId)}`).then(normalizeDeploymentRun)
}

export async function listDeploymentRuns(sessionId: string): Promise<DeploymentRunRecord[]> {
  const payload = await http.get<unknown>('/api/deployment-runs', { session_id: sessionId, limit: 20 })
  const envelope = record(payload)
  const values = Array.isArray(payload)
    ? payload
    : Array.isArray(envelope.runs)
      ? envelope.runs
      : Array.isArray(envelope.deployment_runs) ? envelope.deployment_runs : []
  return values.map(normalizeDeploymentRun)
}

export function confirmDeploymentPlan(runId: string, planHash: string) {
  return http.post<unknown>(`/api/deployment-runs/${encodeURIComponent(runId)}/confirm`, {
    plan_hash: planHash,
  }).then(normalizeDeploymentRun)
}

export function cancelDeploymentRun(runId: string) {
  return http.post<unknown>(`/api/deployment-runs/${encodeURIComponent(runId)}/cancel`).then(normalizeDeploymentRun)
}

export function confirmDeploymentRollback(runId: string) {
  return http.post<unknown>(`/api/deployment-runs/${encodeURIComponent(runId)}/rollback/confirm`).then(normalizeDeploymentRun)
}
