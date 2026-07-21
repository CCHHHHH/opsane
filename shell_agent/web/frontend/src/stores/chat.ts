import { defineStore } from 'pinia'

import type {
  CommandPreviewEvent,
  ConfirmAckEvent,
  ConfirmMode,
  FileTransferConfirmAckEvent,
  FileTransferPreviewEvent,
  OperationPlanEvent,
  PersistedMessage,
  SessionSyncEvent,
  SessionUpdatedEvent,
  ServerEvent,
  TurnStateEvent,
} from '../api/protocol'
import { pendingCommandEvent, pendingFileTransferEvent, pendingOperationPlanEvent, previewNeedsConfirmation } from '../api/protocol'
import { workbenchSocket, type ConnectionState } from '../api/websocket'
import { loadChatConfirmMode, saveChatConfirmMode } from '../utils/chatPreferences'
import { useSessionsStore } from './sessions'

export interface TimelineEntry {
  id: string
  event: ServerEvent
  /** Stable client-side ordering time for realtime and restored messages. */
  receivedAt?: number
}

function timelineTimestamp(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value !== 'string' || !value.trim()) return undefined
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : undefined
}

let subscriptionsReady = false

export const useChatStore = defineStore('chat', {
  state: () => ({
    sessionId: '',
    entries: [] as TimelineEntry[],
    target: '',
    confirmMode: loadChatConfirmMode(),
    connectionState: 'idle' as ConnectionState,
    turnStates: {} as Record<string, TurnStateEvent>,
    activePlan: null as OperationPlanEvent | null,
    pendingPreview: null as CommandPreviewEvent | null,
    confirmingTaskId: '',
    pendingFileTransfer: null as FileTransferPreviewEvent | null,
    confirmingFileTransferId: '',
  }),
  getters: {
    busy: (state) => Object.values(state.turnStates).some((turn) => turn.active),
    confirmationSubmitting: (state) => Boolean(state.confirmingTaskId),
    fileTransferConfirmationSubmitting: (state) => Boolean(state.confirmingFileTransferId),
  },
  actions: {
    setConfirmMode(mode: ConfirmMode) {
      this.confirmMode = mode
      saveChatConfirmMode(mode)
    },
    connect() {
      if (!subscriptionsReady) {
        workbenchSocket.subscribe((event) => this.consume(event))
        workbenchSocket.subscribeState((state) => {
          this.connectionState = state
          if (state === 'open') this.subscribeSession()
        })
        subscriptionsReady = true
      }
      workbenchSocket.connect()
    },
    subscribeSession() {
      if (!this.sessionId || !['open', 'connecting', 'reconnecting'].includes(this.connectionState)) return
      workbenchSocket.send({
        type: 'subscribe',
        session_id: this.sessionId,
        channel: 'chat',
      })
    },
    setSession(sessionId: string) {
      if (this.sessionId === sessionId) return
      this.sessionId = sessionId
      this.entries = []
      this.turnStates = {}
      this.activePlan = null
      this.pendingPreview = null
      this.confirmingTaskId = ''
      this.pendingFileTransfer = null
      this.confirmingFileTransferId = ''
      this.target = ''
      this.subscribeSession()
    },
    hydrate(messages: PersistedMessage[] = []) {
      this.entries = messages.map((message, index) => {
        let payload: Record<string, unknown> = {}
        if (message.payload && typeof message.payload === 'object') payload = message.payload
        if (typeof message.payload === 'string') {
          try { payload = JSON.parse(message.payload) as Record<string, unknown> } catch { payload = {} }
        }
        const fallbackType = message.role === 'user' ? 'user_message' : message.role === 'system' ? 'system' : 'agent'
        const event = {
          ...payload,
          type: message.message_type ?? message.type ?? fallbackType,
          content: message.content ?? payload.content ?? '',
          timestamp: message.timestamp ?? message.created_at,
          session_id: this.sessionId,
        } as ServerEvent
        return {
          id: `history-${message.id ?? index}`,
          event,
          receivedAt: timelineTimestamp(message.created_at ?? message.timestamp),
        }
      })
    },
    restorePending(pending: Record<string, unknown> | null | undefined) {
      this.confirmingTaskId = ''
      this.confirmingFileTransferId = ''
      this.pendingPreview = pendingCommandEvent(pending?.chat, this.sessionId, 'chat')
      if (this.pendingPreview && !previewNeedsConfirmation(this.pendingPreview)) this.pendingPreview = null
      this.pendingFileTransfer = pendingFileTransferEvent(pending?.file_transfer, this.sessionId)
      this.activePlan = pendingOperationPlanEvent(pending?.operation_plan, this.sessionId)

      const active = this.pendingPreview ?? this.pendingFileTransfer ?? this.activePlan
      if (active?.turn_id) {
        this.turnStates[active.turn_id] = {
          type: 'turn_state',
          session_id: this.sessionId,
          turn_id: active.turn_id,
          channel: 'chat',
          status: 'waiting_confirm',
          label: this.activePlan
            ? '等待确认方案'
            : this.pendingFileTransfer
              ? '等待确认文件上传'
              : '等待人工确认',
          active: true,
        }
      }
    },
    restoreTasks(tasks: Array<Record<string, unknown>> = []) {
      this.turnStates = {}
      const task = tasks.find((item) => (item.channel ?? 'chat') === 'chat')
      if (!task || typeof task.id !== 'string' || typeof task.status !== 'string') return
      const events = Array.isArray(task.events) ? task.events : []
      const stateEvent = [...events].reverse().find((item) => {
        return item && typeof item === 'object' && (item as Record<string, unknown>).type === 'turn_state'
      }) as Record<string, unknown> | undefined
      const payload = stateEvent?.payload && typeof stateEvent.payload === 'object'
        ? stateEvent.payload as Record<string, unknown>
        : {}
      const defaultLabels: Record<string, string> = {
        pending: '准备中', thinking: '正在思考', planning: '等待确认方案',
        waiting_confirm: '等待人工确认', confirming: '正在提交确认',
        executing: '正在执行命令', running: '正在执行任务',
        analyzing: '正在分析结果',
      }
      this.turnStates[task.id] = {
        type: 'turn_state',
        session_id: this.sessionId,
        turn_id: task.id,
        channel: 'chat',
        status: task.status,
        label: typeof payload.label === 'string' ? payload.label : (defaultLabels[task.status] ?? '任务进行中'),
        active: true,
      }
    },
    consume(event: ServerEvent) {
      if (event.session_id && event.session_id !== this.sessionId) return
      if (event.type === 'session_sync') {
        const sync = event as SessionSyncEvent
        this.hydrate(sync.messages)
        this.restoreTasks(sync.tasks)
        this.restorePending(sync.pending)
        return
      }
      if (event.type === 'session_updated') {
        useSessionsStore().applyRealtimeUpdate(event as SessionUpdatedEvent)
        return
      }
      if ((event.channel ?? 'chat') === 'command' || event.type === 'completion_result' || event.type === 'command_error') return
      if (event.type === 'confirm_ack') {
        const ack = event as ConfirmAckEvent
        const taskMatches = Boolean(this.confirmingTaskId)
          && (!ack.task_id || ack.task_id === this.confirmingTaskId)
        if (!taskMatches) return
        const accepted = ack.accepted ?? ack.ok ?? ack.success ?? true
        this.confirmingTaskId = ''
        if (accepted || ack.status === 'not_found') this.pendingPreview = null
        return
      }
      if (event.type === 'file_transfer_confirm_ack') {
        const ack = event as FileTransferConfirmAckEvent
        const transferMatches = Boolean(this.confirmingFileTransferId)
          && (!ack.transfer_id || ack.transfer_id === this.confirmingFileTransferId)
        if (!transferMatches) return
        const accepted = ack.accepted ?? true
        this.confirmingFileTransferId = ''
        if (accepted || ack.status === 'not_found') this.pendingFileTransfer = null
        return
      }
      if (event.type === 'turn_state') {
        const turn = event as TurnStateEvent
        this.turnStates[turn.turn_id] = turn
        if (this.confirmingTaskId === turn.turn_id && turn.status !== 'waiting_confirm') {
          this.confirmingTaskId = ''
          this.pendingPreview = null
        }
        if (this.pendingFileTransfer?.turn_id === turn.turn_id && turn.status !== 'waiting_confirm') {
          this.confirmingFileTransferId = ''
          this.pendingFileTransfer = null
        }
        const waitingForPlan = turn.active
          && turn.status === 'planning'
          && turn.label.includes('确认方案')
        if (waitingForPlan && !this.activePlan) {
          const latestPlan = [...this.entries].reverse().find((entry) => (
            entry.event.type === 'operation_plan'
            && entry.event.turn_id === turn.turn_id
          ))?.event as OperationPlanEvent | undefined
          if (latestPlan) this.activePlan = latestPlan
        } else if (
          this.activePlan?.turn_id === turn.turn_id
          && !['planning', 'waiting_confirm'].includes(turn.status)
        ) {
          this.activePlan = null
        }
      } else if (event.type === 'operation_plan') {
        const plan = event as OperationPlanEvent
        this.activePlan = plan.active ? plan : null
      } else if (event.type === 'command_preview') {
        const preview = event as CommandPreviewEvent
        this.activePlan = null
        this.confirmingTaskId = ''
        this.pendingPreview = previewNeedsConfirmation(preview) ? preview : null
      } else if (event.type === 'file_transfer_preview') {
        const preview = event as FileTransferPreviewEvent
        this.activePlan = null
        this.confirmingFileTransferId = ''
        // File writes always require confirmation, including full_access.
        this.pendingFileTransfer = preview
      } else if (event.type === 'artifact_upload') {
        const artifact = event.artifact && typeof event.artifact === 'object'
          ? event.artifact as Record<string, unknown>
          : {}
        const transferId = String(artifact.transfer_id ?? artifact.id ?? '')
        const pendingId = String(this.pendingFileTransfer?.transfer.id ?? '')
        if (pendingId && (!transferId || transferId === pendingId)) {
          this.confirmingFileTransferId = ''
          this.pendingFileTransfer = null
        }
      } else if (event.type === 'task_step') {
        const taskId = typeof event.task_id === 'string' ? event.task_id : ''
        const status = String(event.status ?? '')
        if (taskId === this.confirmingTaskId && status !== 'pending') {
          this.confirmingTaskId = ''
          this.pendingPreview = null
        }
      } else if (event.type === 'execution_result' || event.type === 'system') {
        if (event.type === 'execution_result') {
          this.confirmingTaskId = ''
          this.pendingPreview = null
        } else if (this.confirmingTaskId && String(event.content ?? '').includes('二次确认不匹配')) {
          this.confirmingTaskId = ''
        } else if (this.confirmingTaskId && String(event.content ?? '').includes('无待确认的命令')) {
          this.confirmingTaskId = ''
          this.pendingPreview = null
        }
      }
      this.entries.push({
        id: `${event.timestamp ?? Date.now()}-${event.type}-${this.entries.length}`,
        event,
        // Realtime transport timestamps are intentionally compact (HH:mm:ss),
        // so capture the receipt time for chronological UI projections.
        receivedAt: Date.now(),
      })
    },
    sendMessage(message: string) {
      const trimmed = message.trim()
      if (!trimmed || !this.sessionId) return
      workbenchSocket.send({
        type: 'chat',
        session_id: this.sessionId,
        message: trimmed,
        target: '',
        confirm_mode: this.confirmMode,
      })
    },
    confirm(confirmed: boolean, secondaryConfirmValue = '') {
      const taskId = this.pendingPreview?.task_id ?? ''
      if (!this.sessionId || !taskId || this.confirmingTaskId) return false
      const submitted = workbenchSocket.send({
        type: 'confirm',
        session_id: this.sessionId,
        confirmed,
        channel: 'chat',
        task_id: taskId,
        operation_id: taskId,
        request_id: `confirm-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`,
        secondary_confirm_value: secondaryConfirmValue,
      })
      if (submitted) this.confirmingTaskId = taskId
      return submitted
    },
    confirmFileTransfer(confirmed: boolean) {
      const transferId = String(this.pendingFileTransfer?.transfer.id ?? '')
      if (!this.sessionId || !transferId || this.confirmingFileTransferId) return false
      const submitted = workbenchSocket.send({
        type: 'file_transfer_confirm',
        session_id: this.sessionId,
        transfer_id: transferId,
        confirmed,
        request_id: `file-transfer-confirm-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`,
      })
      if (submitted) this.confirmingFileTransferId = transferId
      return submitted
    },
    confirmPlan(confirmed: boolean) {
      if (!this.sessionId || !this.activePlan) return
      workbenchSocket.send({
        type: 'plan_confirm',
        session_id: this.sessionId,
        plan_id: this.activePlan.plan_id,
        confirmed,
      })
      this.activePlan = null
    },
    adjustPlan(instruction: string) {
      if (!this.sessionId || !this.activePlan || !instruction.trim()) return
      workbenchSocket.send({
        type: 'plan_adjust',
        session_id: this.sessionId,
        plan_id: this.activePlan.plan_id,
        instruction: instruction.trim(),
      })
    },
    cancel() {
      if (!this.sessionId) return
      workbenchSocket.send({ type: 'cancel', session_id: this.sessionId, channel: 'chat' })
    },
  },
})
