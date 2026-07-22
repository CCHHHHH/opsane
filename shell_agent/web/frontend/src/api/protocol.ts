export type ConfirmMode = 'interactive' | 'dry_run' | 'auto_safe' | 'full_access'
export type SessionType = 'chat' | 'command'
export type MessageChannel = 'chat' | 'command'

export interface CurrentServer {
  alias: string
  host: string
  env: string
}

export interface RuntimeState {
  current_server: CurrentServer | null
  stats: {
    executed: number
    failed: number
  }
}

export interface SessionSummary {
  id: string
  type: SessionType
  title: string
  created_at?: string
  updated_at?: string
  last_message_at?: string
  message_count?: number
  pinned_at?: string | null
  [key: string]: unknown
}

export interface PersistedMessage {
  id?: string | number
  role?: 'user' | 'assistant' | 'system'
  type?: string
  message_type?: string
  content?: string
  payload?: Record<string, unknown> | string | null
  created_at?: string
  timestamp?: string
  [key: string]: unknown
}

export interface SessionDetail extends SessionSummary {
  messages?: PersistedMessage[]
  pending?: Record<string, unknown> | null
  tasks?: Array<Record<string, unknown>>
}

export interface ServerRecord {
  alias: string
  host: string
  port?: number
  env?: string
  role?: string
  ssh_credential?: string
  ssh_username?: string
  ssh_auth_type?: string
  ssh_password_set?: boolean
  ssh_private_key_set?: boolean
  ssh_passphrase_set?: boolean
  tags?: string[]
  [key: string]: unknown
}

export interface ServiceRecord {
  id: string
  name: string
  env?: string
  owners?: string[]
  servers?: string[]
  deploy_dir?: string
  artifact_path?: string
  backup_dir?: string
  artifact_type?: string
  startup_timeout_seconds?: number
  log_dir?: string
  health_url?: string
  ports?: number[]
  tags?: string[]
  start_cmd?: string
  stop_cmd?: string
  restart_cmd?: string
  status_cmd?: string
  config_paths?: string[]
  runtime?: string
  version?: string
  last_verified_at?: string
  verification_status?: 'verified' | 'stale' | 'conflicted' | 'unknown'
  source_task_id?: string
  revision?: number
  notes?: string
  [key: string]: unknown
}

export interface CredentialRecord {
  id: string
  type?: string
  username?: string
  password_set?: boolean
  private_key_set?: boolean
  passphrase_set?: boolean
  [key: string]: unknown
}

export interface MemoryRecord {
  id: string | number
  subject?: string
  predicate?: string
  value?: string
  target?: string
  type?: 'fact' | 'procedure' | 'preference' | 'observation'
  status?: 'inferred' | 'confirmed' | 'promoted' | 'stale' | 'conflicted'
  confidence?: number
  source_session_id?: string
  source_task_id?: string
  source_event_id?: string
  source?: string
  observed_at?: string
  expires_at?: string
  evidence_summary?: string
  fingerprint?: string
  created_at?: string
  updated_at?: string
  [key: string]: unknown
}

export interface ProfileCandidateRecord {
  id: string
  service_id?: string
  service_name: string
  proposed_changes: Record<string, unknown>
  before_snapshot?: Record<string, unknown>
  evidence?: Record<string, unknown>
  confidence?: number
  fingerprint?: string
  status?: 'pending' | 'accepted' | 'rejected' | 'expired'
  source_memory_ids?: string[]
  source_task_id?: string
  created_at?: string
  reviewed_at?: string
  reviewed_by?: string
}

export interface SkillRecord {
  name: string
  description?: string
  enabled?: boolean
  triggers?: string[]
  [key: string]: unknown
}

export interface AuditRecord {
  id?: string | number
  timestamp?: string
  created_at?: string
  command?: string
  target?: string
  target_env?: string
  executor?: string
  source?: string
  executed?: boolean
  user_confirmed?: boolean | null
  exit_code?: number | null
  duration_ms?: number | null
  timed_out?: boolean
  [key: string]: unknown
}

interface ClientBaseEvent {
  session_id: string
}

export type ClientEvent =
  | (ClientBaseEvent & {
      type: 'subscribe'
      channel: MessageChannel
    })
  | (ClientBaseEvent & {
      type: 'chat'
      message: string
      confirm_mode: ConfirmMode
      target?: string
    })
  | (ClientBaseEvent & {
      type: 'command'
      command: string
      confirm_mode: ConfirmMode
      target?: string
      cwd?: string
    })
  | (ClientBaseEvent & {
      type: 'confirm'
      confirmed: boolean
      channel: MessageChannel
      task_id?: string
      operation_id?: string
      request_id?: string
      secondary_confirm_value?: string
    })
  | (ClientBaseEvent & {
      type: 'plan_confirm'
      plan_id: string
      confirmed: boolean
    })
  | (ClientBaseEvent & {
      type: 'plan_adjust'
      plan_id: string
      instruction: string
    })
  | (ClientBaseEvent & {
      type: 'file_transfer_confirm'
      transfer_id: string
      confirmed: boolean
      request_id: string
    })
  | (ClientBaseEvent & {
      type: 'complete'
      command: string
      cursor: number
      target?: string
      cwd?: string
      request_id?: string
      input_id?: string
    })
  | (ClientBaseEvent & {
      type: 'cancel'
      channel: MessageChannel
    })

interface ServerBaseEvent {
  type: string
  timestamp?: string
  session_id?: string
  turn_id?: string
  channel?: MessageChannel
  [key: string]: unknown
}

export interface TextServerEvent extends ServerBaseEvent {
  type: 'user_message' | 'agent' | 'system' | 'command_error'
  content: string
}

export interface CommandPreviewEvent extends ServerBaseEvent {
  type: 'command_preview'
  task_id: string
  operation_id?: string
  step_index?: number
  total_steps?: number
  command: string
  target: string
  cwd: string
  intent: string
  explanation: string
  confirm_mode: ConfirmMode
  risk_level: 'safe' | 'caution' | 'dangerous' | 'critical'
  risk_reasons: string[]
  risk_rules: string[]
  policy_blocked: boolean
  policy_block_reason: string
  requires_secondary_confirm: boolean
  secondary_confirm_label: string
  secondary_confirm_expected: string
  secondary_confirm_reason: string
}

export interface ConfirmPromptEvent extends ServerBaseEvent {
  type: 'confirm_prompt'
  content: string
}

/** Optional transport acknowledgement for a command confirmation request. */
export interface ConfirmAckEvent extends ServerBaseEvent {
  type: 'confirm_ack'
  task_id?: string
  operation_id?: string
  request_id?: string
  confirmed?: boolean
  accepted?: boolean
  duplicate?: boolean
  status?: string
  ok?: boolean
  success?: boolean
  content?: string
}

export interface ExecutionStatusEvent extends ServerBaseEvent {
  type: 'execution_status'
  status: string
  content: string
}

export interface ExecutionResultEvent extends ServerBaseEvent {
  type: 'execution_result'
  task_id?: string
  success: boolean
  partial_success?: boolean
  output: string
  exit_code: number
  timed_out?: boolean
  command?: string
  target?: string
  cwd?: string
}

export interface TaskStepEvent extends ServerBaseEvent {
  type: 'task_step'
  task_id?: string
  step_index?: number
  total_steps?: number
  status?: string
  content?: string
  intent?: string
  command?: string
  target?: string
}

export interface TurnStateEvent extends ServerBaseEvent {
  type: 'turn_state'
  turn_id: string
  channel: 'chat'
  status: 'thinking' | 'planning' | 'waiting_confirm' | 'executing' | 'completed' | 'failed' | 'canceled' | 'blocked' | 'timeout' | string
  label: string
  active: boolean
}

export interface OperationPlanStep {
  command: string
  intent: string
  explanation: string
  target?: string
  [key: string]: unknown
}

export interface OperationPlanEvent extends ServerBaseEvent {
  type: 'operation_plan'
  channel: 'chat'
  plan_id: string
  active: boolean
  intent: string
  title: string
  goal: string
  recommended_approach: string
  impact: string[]
  risks: string[]
  rollback: string[]
  verification: string[]
  steps: OperationPlanStep[]
}

export interface CompletionResultEvent extends ServerBaseEvent {
  type: 'completion_result'
  request_id?: string
  input_id?: string
  kind?: string
  start: number
  end: number
  prefix?: string
  candidates: string[]
  common_prefix?: string
}

export interface SessionUpdatedEvent extends ServerBaseEvent {
  type: 'session_updated'
  session_id: string
  title: string
  session_type: SessionType
}

export interface SessionSyncEvent extends ServerBaseEvent {
  type: 'session_sync'
  session_id: string
  channel: MessageChannel
  messages: PersistedMessage[]
  pending: Record<string, unknown>
  tasks: Array<Record<string, unknown>>
}

export interface ArtifactUploadRecord {
  id?: string
  file_id?: string
  file_name?: string
  filename?: string
  target?: string
  remote_dir?: string
  remote_name?: string
  remote_path?: string
  size?: number
  sha256?: string
  remote_size?: number
  remote_sha256?: string
  status?: string
  error?: string
  completed_at?: string
  [key: string]: unknown
}

export interface FileTransferPreviewEvent extends ServerBaseEvent {
  type: 'file_transfer_preview'
  channel: 'chat'
  turn_id: string
  transfer: ArtifactUploadRecord
  requires_confirmation: true
  confirm_mode: 'interactive'
}

export interface FileTransferConfirmAckEvent extends ServerBaseEvent {
  type: 'file_transfer_confirm_ack'
  transfer_id: string
  request_id?: string
  confirmed?: boolean
  accepted?: boolean
  duplicate?: boolean
  status?: string
  content?: string
  transfer?: ArtifactUploadRecord
}

export interface ArtifactUploadEvent extends ServerBaseEvent {
  type: 'artifact_upload'
  content?: string
  artifact?: ArtifactUploadRecord
}

export type ServerEvent =
  | TextServerEvent
  | CommandPreviewEvent
  | ConfirmPromptEvent
  | ConfirmAckEvent
  | ExecutionStatusEvent
  | ExecutionResultEvent
  | TaskStepEvent
  | TurnStateEvent
  | OperationPlanEvent
  | CompletionResultEvent
  | SessionUpdatedEvent
  | SessionSyncEvent
  | FileTransferPreviewEvent
  | FileTransferConfirmAckEvent
  | ArtifactUploadEvent
  | ServerBaseEvent

export function isServerEvent(value: unknown): value is ServerEvent {
  return Boolean(value && typeof value === 'object' && typeof (value as { type?: unknown }).type === 'string')
}

export function previewNeedsConfirmation(preview: CommandPreviewEvent): boolean {
  if (preview.policy_blocked || preview.confirm_mode === 'full_access' || preview.confirm_mode === 'dry_run') return false
  return preview.confirm_mode !== 'auto_safe' || preview.risk_level !== 'safe'
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function stringValue(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : []
}

/** Convert the REST session pending-state shape into a normal WS event. */
export function pendingCommandEvent(
  value: unknown,
  sessionId: string,
  channel: MessageChannel,
): CommandPreviewEvent | null {
  const pending = record(value)
  if (!pending || !stringValue(pending.task_id) || !stringValue(pending.command)) return null
  const mode = stringValue(pending.confirm_mode, 'interactive')
  const risk = stringValue(pending.risk_level, 'caution')
  return {
    ...pending,
    type: 'command_preview',
    session_id: stringValue(pending.session_id, sessionId),
    turn_id: stringValue(pending.turn_id),
    channel,
    task_id: stringValue(pending.task_id),
    command: stringValue(pending.command),
    target: stringValue(pending.target),
    cwd: stringValue(pending.cwd),
    intent: stringValue(pending.intent),
    explanation: stringValue(pending.explanation),
    confirm_mode: ['interactive', 'dry_run', 'auto_safe', 'full_access'].includes(mode)
      ? mode as ConfirmMode
      : 'interactive',
    risk_level: ['safe', 'caution', 'dangerous', 'critical'].includes(risk)
      ? risk as CommandPreviewEvent['risk_level']
      : 'caution',
    risk_reasons: stringList(pending.risk_reasons),
    risk_rules: stringList(pending.risk_rules),
    policy_blocked: Boolean(pending.policy_blocked),
    policy_block_reason: stringValue(pending.policy_block_reason),
    requires_secondary_confirm: Boolean(pending.requires_secondary_confirm),
    secondary_confirm_label: stringValue(pending.secondary_confirm_label),
    secondary_confirm_expected: stringValue(pending.secondary_confirm_expected),
    secondary_confirm_reason: stringValue(pending.secondary_confirm_reason),
  }
}

/** Match the one concrete command that is currently waiting for confirmation. */
export function commandPreviewsMatch(
  candidate: CommandPreviewEvent,
  pending: CommandPreviewEvent,
): boolean {
  if (candidate.task_id !== pending.task_id) return false

  const candidateOperationId = stringValue(candidate.operation_id)
  const pendingOperationId = stringValue(pending.operation_id)
  if (candidateOperationId || pendingOperationId) {
    return Boolean(
      candidateOperationId
      && pendingOperationId
      && candidateOperationId === pendingOperationId
    )
  }

  const candidateStep = Number(candidate.step_index)
  const pendingStep = Number(pending.step_index)
  if (candidateStep > 0 || pendingStep > 0) {
    return candidateStep > 0 && pendingStep > 0 && candidateStep === pendingStep
  }

  return candidate.command === pending.command
    && candidate.target === pending.target
    && candidate.cwd === pending.cwd
}

/** Restore an actionable conversational SFTP preview from session_sync. */
export function pendingFileTransferEvent(value: unknown, sessionId: string): FileTransferPreviewEvent | null {
  const transfer = record(value)
  if (!transfer || stringValue(transfer.status) !== 'waiting_confirm' || !stringValue(transfer.id)) return null
  return {
    type: 'file_transfer_preview',
    session_id: sessionId,
    turn_id: stringValue(transfer.turn_id),
    channel: 'chat',
    transfer: transfer as ArtifactUploadRecord,
    requires_confirmation: true,
    confirm_mode: 'interactive',
  }
}

/** Convert the REST session pending-plan shape into a normal WS event. */
export function pendingOperationPlanEvent(value: unknown, sessionId: string): OperationPlanEvent | null {
  const pending = record(value)
  if (!pending || !stringValue(pending.plan_id)) return null
  const rawSteps = Array.isArray(pending.steps) ? pending.steps : []
  return {
    ...pending,
    type: 'operation_plan',
    session_id: stringValue(pending.session_id, sessionId),
    turn_id: stringValue(pending.turn_id),
    channel: 'chat',
    plan_id: stringValue(pending.plan_id),
    active: true,
    intent: stringValue(pending.intent),
    title: stringValue(pending.title),
    goal: stringValue(pending.goal),
    recommended_approach: stringValue(pending.recommended_approach),
    impact: stringList(pending.impact),
    risks: stringList(pending.risks),
    rollback: stringList(pending.rollback),
    verification: stringList(pending.verification),
    steps: rawSteps.filter((step): step is OperationPlanStep => Boolean(record(step))),
  }
}
