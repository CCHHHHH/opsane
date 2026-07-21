import { describe, expect, it } from 'vitest'

import type { TimelineEntry } from '../stores/chat'
import { groupChatTurns } from './chatTurns'

function entry(id: string, event: TimelineEntry['event']): TimelineEntry {
  return { id, event }
}

describe('chat turn projection', () => {
  it('groups a conversation by turn and merges command preview with its result', () => {
    const turns = groupChatTurns([
      entry('u1', { type: 'user_message', turn_id: 'turn-1', content: '检查 MySQL 版本' }),
      entry('p1', {
        type: 'command_preview', turn_id: 'turn-1', task_id: 'task-1', command: 'mysql --version',
        target: 'dev-01', cwd: '', intent: '检查 MySQL 版本', explanation: '', confirm_mode: 'auto_safe',
        risk_level: 'safe', risk_reasons: [], risk_rules: [], policy_blocked: false, policy_block_reason: '',
        requires_secondary_confirm: false, secondary_confirm_label: '', secondary_confirm_expected: '', secondary_confirm_reason: '',
      }),
      entry('r1', {
        type: 'execution_result', turn_id: 'turn-1', task_id: 'task-1', success: true,
        output: 'mysql Ver 8.0.45', exit_code: 0, command: 'mysql --version', target: 'dev-01',
      }),
      entry('a1', { type: 'agent', turn_id: 'turn-1', content: '## 结论\n\nMySQL 版本为 8.0.45。' }),
    ])

    expect(turns).toHaveLength(1)
    expect(turns[0].summary.title).toBe('检查 MySQL 版本')
    expect(turns[0].summary.status).toBe('success')
    expect(turns[0].summary.chips).toContain('dev-01')
    expect(turns[0].items).toHaveLength(2)
    expect(turns[0].items[0].kind).toBe('execution')
    if (turns[0].items[0].kind === 'execution') {
      expect(turns[0].items[0].preview?.command).toBe('mysql --version')
      expect(turns[0].items[0].result?.output).toBe('mysql Ver 8.0.45')
    }
  })

  it('keeps multiple commands in one task as separate execution steps', () => {
    const turns = groupChatTurns([
      entry('u1', { type: 'user_message', turn_id: 'turn-1', content: '检查两台机器' }),
      entry('p1', {
        type: 'command_preview', turn_id: 'turn-1', task_id: 'task-1', command: 'uptime', target: 'dev-01',
        cwd: '', intent: '检查 dev-01', explanation: '', confirm_mode: 'auto_safe', risk_level: 'safe',
        risk_reasons: [], risk_rules: [], policy_blocked: false, policy_block_reason: '', requires_secondary_confirm: false,
        secondary_confirm_label: '', secondary_confirm_expected: '', secondary_confirm_reason: '',
      }),
      entry('r1', { type: 'execution_result', turn_id: 'turn-1', task_id: 'task-1', success: true, output: 'ok-1', exit_code: 0, command: 'uptime', target: 'dev-01' }),
      entry('p2', {
        type: 'command_preview', turn_id: 'turn-1', task_id: 'task-1', command: 'uptime', target: 'dev-02',
        cwd: '', intent: '检查 dev-02', explanation: '', confirm_mode: 'auto_safe', risk_level: 'safe',
        risk_reasons: [], risk_rules: [], policy_blocked: false, policy_block_reason: '', requires_secondary_confirm: false,
        secondary_confirm_label: '', secondary_confirm_expected: '', secondary_confirm_reason: '',
      }),
      entry('r2', { type: 'execution_result', turn_id: 'turn-1', task_id: 'task-1', success: true, output: 'ok-2', exit_code: 0, command: 'uptime', target: 'dev-02' }),
    ])

    const executions = turns[0].items.filter((item) => item.kind === 'execution')
    expect(executions).toHaveLength(2)
    expect(executions.map((item) => item.kind === 'execution' ? item.result?.target : '')).toEqual(['dev-01', 'dev-02'])
  })

  it('folds pending, running, command, result, and success events into one visible step', () => {
    const turns = groupChatTurns([
      entry('u1', { type: 'user_message', turn_id: 'turn-1', content: '重启服务' }),
      entry('s1', { type: 'task_step', turn_id: 'turn-1', task_id: 'turn-1', step_index: 1, total_steps: 2, status: 'pending', content: '等待确认', intent: '停止服务', command: './stop.sh', target: 'dev-01' }),
      entry('p1', {
        type: 'command_preview', turn_id: 'turn-1', task_id: 'turn-1', command: './stop.sh', target: 'dev-01', cwd: '', intent: '停止服务', explanation: '', confirm_mode: 'auto_safe', risk_level: 'caution',
        risk_reasons: ['停止服务'], risk_rules: [], policy_blocked: false, policy_block_reason: '', requires_secondary_confirm: false, secondary_confirm_label: '', secondary_confirm_expected: '', secondary_confirm_reason: '',
      }),
      entry('s2', { type: 'task_step', turn_id: 'turn-1', task_id: 'turn-1', step_index: 1, total_steps: 2, status: 'running', content: '执行中', intent: '停止服务', command: './stop.sh', target: 'dev-01' }),
      entry('r1', { type: 'execution_result', turn_id: 'turn-1', task_id: 'turn-1', success: true, output: '已停止', exit_code: 0, command: './stop.sh', target: 'dev-01' }),
      entry('s3', { type: 'task_step', turn_id: 'turn-1', task_id: 'turn-1', step_index: 1, total_steps: 2, status: 'success', content: '执行完成', intent: '停止服务', command: './stop.sh', target: 'dev-01' }),
    ])

    expect(turns[0].items).toHaveLength(1)
    const item = turns[0].items[0]
    expect(item.kind).toBe('execution')
    if (item.kind === 'execution') {
      expect(item.taskStep?.status).toBe('success')
      expect(item.preview?.command).toBe('./stop.sh')
      expect(item.result?.output).toBe('已停止')
    }
  })
})
