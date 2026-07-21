import { describe, expect, it } from 'vitest'

import type { DeploymentRunRecord } from '../api/deployments'
import type { TimelineEntry } from '../stores/chat'
import { groupChatTurns } from './chatTurns'
import { projectChatTimeline } from './chatTimeline'

function message(id: string, turnId: string, content: string, receivedAt: number): TimelineEntry {
  return {
    id,
    receivedAt,
    event: { type: 'user_message', turn_id: turnId, content },
  }
}

function deployment(createdAt: string): DeploymentRunRecord {
  return {
    id: 'deprun-1',
    session_id: 'session-1',
    status: 'completed',
    plan_hash: 'hash',
    created_at: createdAt,
    plan: { service: {}, artifact: {}, steps: [] },
    steps: [],
    events: [],
  }
}

describe('chat timeline projection', () => {
  it('keeps later questions and answers below a completed deployment card', () => {
    const turns = groupChatTurns([
      message('u1', 'turn-1', '部署 bedcare-mock', Date.parse('2026-07-17T03:00:00Z')),
      message('u2', 'turn-2', '继续检查日志', Date.parse('2026-07-17T03:10:00Z')),
    ])

    expect(projectChatTimeline(turns, deployment('2026-07-17T03:05:00Z')).map((item) => item.id)).toEqual([
      'turn:turn-1',
      'deployment:deprun-1',
      'turn:turn-2',
    ])
  })

  it('places a deployment before the first turn when it predates the restored history', () => {
    const turns = groupChatTurns([
      message('u1', 'turn-1', '部署后继续提问', Date.parse('2026-07-17T03:10:00Z')),
    ])

    expect(projectChatTimeline(turns, deployment('2026-07-17T03:05:00Z')).map((item) => item.kind)).toEqual([
      'deployment',
      'turn',
    ])
  })

  it('falls back to the end when an older deployment record has no creation time', () => {
    const turns = groupChatTurns([
      message('u1', 'turn-1', '历史问题', Date.parse('2026-07-17T03:10:00Z')),
    ])

    expect(projectChatTimeline(turns, deployment('')).map((item) => item.kind)).toEqual([
      'turn',
      'deployment',
    ])
  })
})
