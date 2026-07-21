import { defineStore } from 'pinia'

import { pendingCommandEvent, previewNeedsConfirmation, type CommandPreviewEvent, type CompletionResultEvent, type PersistedMessage, type ServerEvent, type SessionSyncEvent, type SessionUpdatedEvent } from '../api/protocol'
import { workbenchSocket, type ConnectionState } from '../api/websocket'
import { loadTerminalSnapshot, MAX_TERMINAL_HISTORY, MAX_TERMINAL_SCROLLBACK, removeTerminalSnapshot, saveTerminalSnapshot } from '../utils/terminalPersistence'
import { useSessionsStore } from './sessions'

export interface TerminalEntry {
  id: string
  event: ServerEvent
}

let subscriptionsReady = false
export const TERMINAL_CONFIRM_MODE = 'auto_safe' as const

export const useTerminalStore = defineStore('terminal', {
  state: () => ({
    sessionId: '',
    entries: [] as TerminalEntry[],
    history: [] as string[],
    target: '',
    cwd: '',
    connectionState: 'idle' as ConnectionState,
    completion: null as CompletionResultEvent | null,
    pendingTaskId: '',
    pendingPreview: null as CommandPreviewEvent | null,
  }),
  actions: {
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
        channel: 'command',
      })
    },
    setSession(sessionId: string) {
      if (this.sessionId === sessionId) return
      this.persistTarget()
      this.sessionId = sessionId
      this.completion = null
      this.pendingTaskId = ''
      this.pendingPreview = null
      this.restoreTarget()
      this.subscribeSession()
    },
    hydrate(messages: PersistedMessage[] = []) {
      const restored = messages.map((message, index) => {
        let payload: Record<string, unknown> = {}
        if (message.payload && typeof message.payload === 'object') payload = message.payload
        if (typeof message.payload === 'string') {
          try { payload = JSON.parse(message.payload) as Record<string, unknown> } catch { payload = {} }
        }
        const event = {
          ...payload,
          type: message.message_type ?? message.type ?? 'system',
          content: message.content ?? payload.content ?? '',
          timestamp: message.timestamp ?? message.created_at,
          session_id: this.sessionId,
          channel: 'command',
        } as ServerEvent
        return { id: `history-${message.id ?? index}`, event }
      })
      if (restored.length) this.entries = restored.slice(-MAX_TERMINAL_SCROLLBACK)
      this.persistTarget()
    },
    restorePending(pending: Record<string, unknown> | null | undefined) {
      const preview = pendingCommandEvent(pending?.command, this.sessionId, 'command')
      this.pendingPreview = preview && previewNeedsConfirmation(preview) ? preview : null
      this.pendingTaskId = this.pendingPreview?.task_id ?? ''
    },
    selectTarget(target: string) {
      if (this.target === target) return
      this.persistTarget()
      this.target = target
      this.completion = null
      this.pendingTaskId = ''
      this.pendingPreview = null
      this.restoreTarget()
    },
    restoreTarget() {
      const scope = this.snapshotScope()
      let snapshot = loadTerminalSnapshot(scope)
      // Migrate the first pre-session-scoping snapshot without copying it into
      // every command session that happens to use the same target.
      if (!snapshot && this.sessionId) {
        snapshot = loadTerminalSnapshot(this.target)
        if (snapshot) {
          saveTerminalSnapshot(scope, snapshot)
          removeTerminalSnapshot(this.target)
        }
      }
      this.cwd = snapshot?.cwd ?? ''
      this.history = snapshot?.history ?? []
      this.entries = (snapshot?.events ?? []).map((event, index) => ({
        id: `local-${this.target || 'default'}-${index}-${event.timestamp ?? ''}`,
        event,
      }))
    },
    persistTarget() {
      saveTerminalSnapshot(this.snapshotScope(), {
        cwd: this.cwd,
        history: this.history,
        events: this.entries.map((entry) => entry.event),
      })
    },
    snapshotScope() {
      return `${this.sessionId || '__no_session__'}::${this.target || '__default__'}`
    },
    consume(event: ServerEvent) {
      if (event.session_id && event.session_id !== this.sessionId) return
      if (event.type === 'session_sync') {
        const sync = event as SessionSyncEvent
        this.hydrate(sync.messages)
        this.restorePending(sync.pending)
        return
      }
      if (event.type === 'session_updated') {
        useSessionsStore().applyRealtimeUpdate(event as SessionUpdatedEvent)
        return
      }
      if (event.channel !== 'command' && event.type !== 'command_error' && event.type !== 'completion_result') return
      if (event.type === 'completion_result') {
        this.completion = event as CompletionResultEvent
        return
      }
      if (event.type === 'command_preview' && typeof event.task_id === 'string') {
        const preview = event as CommandPreviewEvent
        if (previewNeedsConfirmation(preview)) {
          this.pendingTaskId = event.task_id
          this.pendingPreview = preview
        } else {
          this.pendingTaskId = ''
          this.pendingPreview = null
        }
      }
      if (event.type === 'execution_result') {
        if (typeof event.cwd === 'string') this.cwd = event.cwd
        this.pendingTaskId = ''
        this.pendingPreview = null
      }
      this.entries.push({
        id: `${event.timestamp ?? Date.now()}-${event.type}-${this.entries.length}`,
        event,
      })
      if (this.entries.length > MAX_TERMINAL_SCROLLBACK) {
        this.entries.splice(0, this.entries.length - MAX_TERMINAL_SCROLLBACK)
      }
      this.persistTarget()
    },
    run(command: string) {
      const trimmed = command.trim()
      if (!trimmed || !this.sessionId) return
      this.history = [trimmed, ...this.history.filter((item) => item !== trimmed)].slice(0, MAX_TERMINAL_HISTORY)
      this.persistTarget()
      workbenchSocket.send({
        type: 'command',
        session_id: this.sessionId,
        command: trimmed,
        target: this.target,
        cwd: this.cwd,
        confirm_mode: TERMINAL_CONFIRM_MODE,
      })
    },
    confirm(confirmed: boolean, secondaryConfirmValue = '') {
      if (!this.sessionId) return
      workbenchSocket.send({
        type: 'confirm',
        session_id: this.sessionId,
        confirmed,
        channel: 'command',
        task_id: this.pendingTaskId,
        secondary_confirm_value: secondaryConfirmValue,
      })
      if (!confirmed) this.pendingTaskId = ''
      if (!confirmed) this.pendingPreview = null
    },
    complete(command: string, cursor: number, requestId: string) {
      if (!this.sessionId) return
      workbenchSocket.send({
        type: 'complete',
        session_id: this.sessionId,
        command,
        cursor,
        target: this.target,
        cwd: this.cwd,
        request_id: requestId,
        input_id: 'terminal-command',
      })
    },
    cancel() {
      if (!this.sessionId) return
      workbenchSocket.send({ type: 'cancel', session_id: this.sessionId, channel: 'command' })
    },
  },
})
