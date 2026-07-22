import { describe, expect, it } from 'vitest'

import {
  commandPreviewsMatch,
  isServerEvent,
  pendingCommandEvent,
  pendingFileTransferEvent,
} from './protocol'

describe('isServerEvent', () => {
  it('accepts typed and forward-compatible events', () => {
    expect(isServerEvent({ type: 'turn_state', status: 'thinking' })).toBe(true)
    expect(isServerEvent({ type: 'future_event', payload: 1 })).toBe(true)
  })

  it('rejects malformed websocket payloads', () => {
    expect(isServerEvent(null)).toBe(false)
    expect(isServerEvent([])).toBe(false)
    expect(isServerEvent({ status: 'thinking' })).toBe(false)
    expect(isServerEvent({ type: 42 })).toBe(false)
  })

  it('restores only durable waiting file-transfer previews', () => {
    const preview = pendingFileTransferEvent({
      id: 'xfer-1', turn_id: 'turn-1', file_name: 'release.jar', target: 'dev-01',
      remote_path: '/tmp/release.jar', status: 'waiting_confirm',
    }, 'session-1')

    expect(preview).toMatchObject({
      type: 'file_transfer_preview', session_id: 'session-1', turn_id: 'turn-1',
      requires_confirmation: true, confirm_mode: 'interactive',
      transfer: { id: 'xfer-1', status: 'waiting_confirm' },
    })
    expect(pendingFileTransferEvent({ id: 'xfer-1', status: 'running' }, 'session-1')).toBeNull()
    expect(pendingFileTransferEvent({}, 'session-1')).toBeNull()
  })

  it('matches only the concrete command operation waiting for confirmation', () => {
    const completedStep = pendingCommandEvent({
      task_id: 'task-1', operation_id: 'cmd-2', step_index: 2,
      command: '/app/stop.sh', target: 'dev-01',
    }, 'session-1', 'chat')!
    const pendingStep = pendingCommandEvent({
      task_id: 'task-1', operation_id: 'cmd-3', step_index: 3,
      command: '/app/start.sh', target: 'dev-01',
    }, 'session-1', 'chat')!

    expect(commandPreviewsMatch(completedStep, pendingStep)).toBe(false)
    expect(commandPreviewsMatch(pendingStep, pendingStep)).toBe(true)
  })
})
