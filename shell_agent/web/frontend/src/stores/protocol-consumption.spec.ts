import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { CommandPreviewEvent, TurnStateEvent } from '../api/protocol'
import { workbenchSocket } from '../api/websocket'
import { CHAT_CONFIRM_MODE_STORAGE_KEY } from '../utils/chatPreferences'
import { useChatStore } from './chat'
import { useSessionsStore } from './sessions'
import { TERMINAL_CONFIRM_MODE, useTerminalStore } from './terminal'

beforeEach(() => {
  window.localStorage.clear()
  setActivePinia(createPinia())
})

describe('protocol consumers', () => {
  it('defaults chat to automatic target selection with safe-command auto execution', () => {
    const chat = useChatStore()
    expect(chat.target).toBe('')
    expect(chat.confirmMode).toBe('auto_safe')
  })

  it('restores the selected chat confirmation mode after the store is recreated', () => {
    const chat = useChatStore()
    chat.setConfirmMode('full_access')

    expect(window.localStorage.getItem(CHAT_CONFIRM_MODE_STORAGE_KEY)).toBe('full_access')

    setActivePinia(createPinia())
    expect(useChatStore().confirmMode).toBe('full_access')
  })

  it('always sends chat requests with automatic target selection', () => {
    const send = vi.spyOn(workbenchSocket, 'send').mockReturnValue(true)
    const chat = useChatStore()
    chat.setSession('chat-auto-target')
    chat.target = 'stale-server-selection'
    chat.sendMessage('检查服务状态')

    expect(send).toHaveBeenCalledWith(expect.objectContaining({
      type: 'chat',
      target: '',
    }))
    send.mockRestore()
  })

  it('fixes terminal execution mode to safe-command auto execution', () => {
    expect(TERMINAL_CONFIRM_MODE).toBe('auto_safe')

    const send = vi.spyOn(workbenchSocket, 'send').mockReturnValue(true)
    const terminal = useTerminalStore()
    terminal.setSession('command-auto-safe')
    terminal.selectTarget('dev-1')
    terminal.run('uptime')

    expect(send).toHaveBeenCalledWith(expect.objectContaining({
      type: 'command',
      confirm_mode: 'auto_safe',
    }))
    send.mockRestore()
  })

  it('tracks chat turn state by turn id', () => {
    const chat = useChatStore()
    chat.setSession('chat-1')
    const thinking: TurnStateEvent = {
      type: 'turn_state',
      timestamp: '10:00:00',
      session_id: 'chat-1',
      turn_id: 'turn-1',
      channel: 'chat',
      status: 'thinking',
      label: '正在思考',
      active: true,
    }
    chat.consume(thinking)
    expect(chat.busy).toBe(true)
    chat.consume({ ...thinking, status: 'completed', label: '任务完成', active: false })
    expect(chat.busy).toBe(false)
  })

  it('subscribes the selected session and settles restored work from a session sync', () => {
    const send = vi.spyOn(workbenchSocket, 'send').mockReturnValue(true)
    const chat = useChatStore()
    chat.connectionState = 'open'
    chat.setSession('chat-refresh')

    expect(send).toHaveBeenCalledWith({
      type: 'subscribe',
      session_id: 'chat-refresh',
      channel: 'chat',
    })

    chat.restoreTasks([{
      id: 'task-refresh',
      channel: 'chat',
      status: 'analyzing',
      events: [],
    }])
    expect(chat.busy).toBe(true)

    chat.consume({
      type: 'session_sync',
      session_id: 'chat-refresh',
      channel: 'chat',
      messages: [{
        id: 'message-complete',
        role: 'assistant',
        message_type: 'agent',
        content: '任务已完成',
      }],
      pending: {},
      tasks: [],
    })

    expect(chat.busy).toBe(false)
    expect(chat.entries).toHaveLength(1)
    expect(chat.entries[0].event.content).toBe('任务已完成')
    send.mockRestore()
  })

  it('applies an automatic chat title without adding metadata to the timeline', () => {
    const sessions = useSessionsStore()
    sessions.items = [{ id: 'chat-title', type: 'chat', title: '新聊天' }]
    sessions.selected = { id: 'chat-title', type: 'chat', title: '新聊天' }
    const chat = useChatStore()
    chat.setSession('chat-title')

    chat.consume({
      type: 'session_updated',
      session_id: 'chat-title',
      session_type: 'chat',
      channel: 'chat',
      title: 'dev-01 磁盘空间',
    })

    expect(sessions.items[0].title).toBe('dev-01 磁盘空间')
    expect(sessions.selected.title).toBe('dev-01 磁盘空间')
    expect(chat.entries).toEqual([])
  })

  it('applies an automatic terminal title without adding metadata to terminal output', () => {
    const sessions = useSessionsStore()
    sessions.items = [{ id: 'command-title', type: 'command', title: '新命令会话' }]
    sessions.selected = { id: 'command-title', type: 'command', title: '新命令会话' }
    const terminal = useTerminalStore()
    terminal.setSession('command-title')

    terminal.consume({
      type: 'session_updated',
      session_id: 'command-title',
      session_type: 'command',
      channel: 'command',
      title: 'dev-01 · df -h',
    })

    expect(sessions.items[0].title).toBe('dev-01 · df -h')
    expect(sessions.selected.title).toBe('dev-01 · df -h')
    expect(terminal.entries).toEqual([])
  })

  it('retains the command preview needed for terminal confirmation', () => {
    const terminal = useTerminalStore()
    terminal.setSession('command-1')
    const preview: CommandPreviewEvent = {
      type: 'command_preview',
      timestamp: '10:00:00',
      session_id: 'command-1',
      turn_id: '',
      channel: 'command',
      task_id: 'task-1',
      command: 'uptime',
      target: 'dev-1',
      cwd: '/tmp',
      intent: '查看负载',
      explanation: '',
      confirm_mode: 'interactive',
      risk_level: 'safe',
      risk_reasons: [],
      risk_rules: [],
      policy_blocked: false,
      policy_block_reason: '',
      requires_secondary_confirm: false,
      secondary_confirm_expected: '',
      secondary_confirm_label: '',
      secondary_confirm_reason: '',
    }
    terminal.consume(preview)
    expect(terminal.pendingTaskId).toBe('task-1')
    expect(terminal.pendingPreview?.command).toBe('uptime')
  })

  it.each([true, false])('locks a chat confirmation immediately and ignores repeated submissions (%s)', (confirmed) => {
    const send = vi.spyOn(workbenchSocket, 'send').mockReturnValue(true)
    const chat = useChatStore()
    chat.setSession('chat-confirm-once')
    chat.consume({
      type: 'command_preview', session_id: 'chat-confirm-once', turn_id: 'task-confirm', channel: 'chat',
      task_id: 'task-confirm', operation_id: 'cmd-confirm', command: 'systemctl restart app', target: 'dev-1', cwd: '',
      intent: '重启服务', explanation: '', confirm_mode: 'auto_safe', risk_level: 'caution',
      risk_reasons: [], risk_rules: [], policy_blocked: false, policy_block_reason: '',
      requires_secondary_confirm: false, secondary_confirm_expected: '', secondary_confirm_label: '',
      secondary_confirm_reason: '',
    })

    expect(chat.confirm(confirmed)).toBe(true)
    expect(chat.confirm(confirmed)).toBe(false)
    expect(chat.confirmingTaskId).toBe('task-confirm')
    expect(chat.confirmationSubmitting).toBe(true)
    expect(send).toHaveBeenCalledTimes(1)
    expect(send).toHaveBeenCalledWith(expect.objectContaining({
      type: 'confirm', task_id: 'task-confirm', operation_id: 'cmd-confirm', confirmed,
      request_id: expect.stringMatching(/^confirm-/),
    }))
    send.mockRestore()
  })

  it.each([true, false])('locks a conversational file-transfer decision immediately and ignores repeats (%s)', (confirmed) => {
    const send = vi.spyOn(workbenchSocket, 'send').mockReturnValue(true)
    const chat = useChatStore()
    chat.setSession('chat-transfer-once')
    chat.setConfirmMode('full_access')
    chat.consume({
      type: 'file_transfer_preview', session_id: 'chat-transfer-once', turn_id: 'turn-transfer', channel: 'chat',
      requires_confirmation: true, confirm_mode: 'interactive',
      transfer: {
        id: 'xfer-1', file_name: 'release.jar', target: 'dev-1',
        remote_path: '/tmp/release.jar', status: 'waiting_confirm',
      },
    })

    expect(chat.pendingFileTransfer?.transfer.id).toBe('xfer-1')
    expect(chat.confirmFileTransfer(confirmed)).toBe(true)
    expect(chat.confirmFileTransfer(confirmed)).toBe(false)
    expect(chat.fileTransferConfirmationSubmitting).toBe(true)
    expect(send).toHaveBeenCalledTimes(1)
    expect(send).toHaveBeenCalledWith(expect.objectContaining({
      type: 'file_transfer_confirm', transfer_id: 'xfer-1', confirmed,
      request_id: expect.stringMatching(/^file-transfer-confirm-/),
    }))
    send.mockRestore()
  })

  it('restores a file-transfer preview and keeps it on a rejected acknowledgement for retry', () => {
    const chat = useChatStore()
    chat.setSession('chat-transfer-restore')
    chat.restorePending({
      file_transfer: {
        id: 'xfer-restore', turn_id: 'turn-restore', file_name: 'release.jar', target: 'dev-1',
        remote_path: '/tmp/release.jar', status: 'waiting_confirm',
      },
    })

    expect(chat.pendingFileTransfer?.transfer.id).toBe('xfer-restore')
    expect(chat.busy).toBe(true)

    const send = vi.spyOn(workbenchSocket, 'send').mockReturnValue(true)
    chat.confirmFileTransfer(true)
    chat.consume({
      type: 'file_transfer_confirm_ack', session_id: 'chat-transfer-restore', channel: 'chat',
      transfer_id: 'xfer-restore', accepted: false, status: 'conflict', content: '请重试',
    })
    expect(chat.fileTransferConfirmationSubmitting).toBe(false)
    expect(chat.pendingFileTransfer?.transfer.id).toBe('xfer-restore')

    chat.confirmFileTransfer(true)
    chat.consume({
      type: 'file_transfer_confirm_ack', session_id: 'chat-transfer-restore', channel: 'chat',
      transfer_id: 'xfer-restore', accepted: true, status: 'pending',
    })
    expect(chat.pendingFileTransfer).toBeNull()
    send.mockRestore()
  })

  it('settles confirmation submission from an optional ack and allows retry after a rejected ack', () => {
    const send = vi.spyOn(workbenchSocket, 'send').mockReturnValue(true)
    const chat = useChatStore()
    chat.setSession('chat-confirm-ack')
    chat.consume({
      type: 'command_preview', session_id: 'chat-confirm-ack', turn_id: 'task-ack', channel: 'chat',
      task_id: 'task-ack', command: 'rm old.log', target: 'dev-1', cwd: '', intent: '清理日志',
      explanation: '', confirm_mode: 'interactive', risk_level: 'caution', risk_reasons: [], risk_rules: [],
      policy_blocked: false, policy_block_reason: '', requires_secondary_confirm: false,
      secondary_confirm_expected: '', secondary_confirm_label: '', secondary_confirm_reason: '',
    })

    chat.confirm(true)
    chat.consume({ type: 'confirm_ack', session_id: 'chat-confirm-ack', channel: 'chat', task_id: 'task-ack', accepted: false })
    expect(chat.confirmationSubmitting).toBe(false)
    expect(chat.pendingPreview?.task_id).toBe('task-ack')

    chat.confirm(true)
    chat.consume({ type: 'confirm_ack', session_id: 'chat-confirm-ack', channel: 'chat', task_id: 'task-ack', accepted: true })
    expect(chat.confirmationSubmitting).toBe(false)
    expect(chat.pendingPreview).toBeNull()
    expect(send).toHaveBeenCalledTimes(2)
    send.mockRestore()
  })

  it('settles confirmation when execution starts even if the backend sends no ack', () => {
    const send = vi.spyOn(workbenchSocket, 'send').mockReturnValue(true)
    const chat = useChatStore()
    chat.setSession('chat-confirm-progress')
    chat.consume({
      type: 'command_preview', session_id: 'chat-confirm-progress', turn_id: 'task-progress', channel: 'chat',
      task_id: 'task-progress', command: 'uptime', target: 'dev-1', cwd: '', intent: '查看负载',
      explanation: '', confirm_mode: 'interactive', risk_level: 'safe', risk_reasons: [], risk_rules: [],
      policy_blocked: false, policy_block_reason: '', requires_secondary_confirm: false,
      secondary_confirm_expected: '', secondary_confirm_label: '', secondary_confirm_reason: '',
    })

    chat.confirm(true)
    chat.consume({
      type: 'turn_state', session_id: 'chat-confirm-progress', turn_id: 'task-progress', channel: 'chat',
      status: 'executing', label: '正在执行命令', active: true,
    })
    expect(chat.confirmationSubmitting).toBe(false)
    expect(chat.pendingPreview).toBeNull()
    send.mockRestore()
  })

  it('restores actionable chat and terminal pending state returned by the session API', () => {
    const pending = {
      chat: {
        task_id: 'chat-task',
        turn_id: 'chat-turn',
        command: 'systemctl restart app',
        target: 'prod-1',
        confirm_mode: 'interactive',
        risk_level: 'dangerous',
      },
      command: {
        task_id: 'command-task',
        command: 'rm old.log',
        target: 'dev-1',
        confirm_mode: 'interactive',
        risk_level: 'caution',
      },
      operation_plan: {
        plan_id: 'plan-1',
        turn_id: 'plan-turn',
        title: '滚动发布',
        steps: [],
      },
    }

    const chat = useChatStore()
    chat.setSession('chat-1')
    chat.restorePending(pending)
    expect(chat.pendingPreview?.task_id).toBe('chat-task')
    expect(chat.activePlan?.plan_id).toBe('plan-1')
    expect(chat.busy).toBe(true)

    const terminal = useTerminalStore()
    terminal.setSession('command-1')
    terminal.restorePending(pending)
    expect(terminal.pendingTaskId).toBe('command-task')
    expect(terminal.pendingPreview?.channel).toBe('command')
  })

  it('restores only backend-reconciled active chat tasks', () => {
    const chat = useChatStore()
    chat.setSession('chat-active')
    chat.restoreTasks([{
      id: 'task-active',
      channel: 'chat',
      status: 'thinking',
      events: [{
        type: 'turn_state',
        payload: { label: '正在生成命令' },
      }],
    }])

    expect(chat.busy).toBe(true)
    expect(chat.turnStates['task-active'].label).toBe('正在生成命令')

    chat.setSession('chat-idle')
    chat.restoreTasks([])
    expect(chat.busy).toBe(false)
  })

  it('clears a confirmed plan and restores it only when the backend returns to planning', () => {
    const send = vi.spyOn(workbenchSocket, 'send').mockReturnValue(true)
    const chat = useChatStore()
    chat.setSession('chat-plan')
    chat.consume({
      type: 'operation_plan', session_id: 'chat-plan', turn_id: 'turn-plan', channel: 'chat',
      plan_id: 'plan-1', active: true, intent: '重启服务', title: '服务重启方案', goal: '重启服务',
      recommended_approach: '', impact: [], risks: [], rollback: [], verification: [], steps: [],
    })

    chat.confirmPlan(true)
    expect(chat.activePlan).toBeNull()

    chat.consume({
      type: 'turn_state', session_id: 'chat-plan', turn_id: 'turn-plan', channel: 'chat',
      status: 'planning', label: '等待确认方案', active: true,
    })
    expect(chat.activePlan?.plan_id).toBe('plan-1')

    chat.consume({
      type: 'turn_state', session_id: 'chat-plan', turn_id: 'turn-plan', channel: 'chat',
      status: 'thinking', label: '正在生成命令步骤', active: true,
    })
    expect(chat.activePlan).toBeNull()
    send.mockRestore()
  })

  it('keeps terminal target snapshots isolated between command sessions', () => {
    const terminal = useTerminalStore()
    terminal.setSession('command-1')
    terminal.selectTarget('prod-1')
    terminal.consume({
      type: 'system',
      session_id: 'command-1',
      channel: 'command',
      content: 'session one output',
    })

    terminal.setSession('command-2')
    expect(terminal.entries).toEqual([])

    terminal.setSession('command-1')
    expect(terminal.entries.map((entry) => entry.event.content)).toEqual(['session one output'])
  })
})
