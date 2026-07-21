import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ExecutionStep from './ExecutionStep.vue'

const preview = {
  type: 'command_preview' as const,
  task_id: 'task-1', command: 'mysql --version', target: 'dev-01', cwd: '/data/app',
  intent: '检查 MySQL 版本', explanation: '', confirm_mode: 'auto_safe' as const,
  risk_level: 'safe' as const, risk_reasons: [], risk_rules: [], policy_blocked: false,
  policy_block_reason: '', requires_secondary_confirm: false, secondary_confirm_label: '',
  secondary_confirm_expected: '', secondary_confirm_reason: '',
}

describe('ExecutionStep', () => {
  it('keeps command, output, status, and a specific result summary in one flow', () => {
    const wrapper = mount(ExecutionStep, {
      props: {
        preview,
        result: {
          type: 'execution_result', task_id: 'task-1', success: true, exit_code: 0,
          command: 'mysql --version', target: 'dev-01', output: 'mysql Ver 8.0.45\nserver ready',
        },
      },
    })

    expect(wrapper.find('.execution-step-command').text()).toContain('mysql --version')
    expect(wrapper.find('.execution-step-head strong').text()).toBe('检查 MySQL 版本')
    expect(wrapper.find('.execution-step-intent').exists()).toBe(false)
    expect(wrapper.find('.execution-step-meta').text()).toBe('目标 dev-01')
    expect(wrapper.find('.execution-step-meta').text()).not.toContain('auto_safe')
    expect(wrapper.find('.execution-step-meta').text()).not.toContain('safe')
    expect(wrapper.find('.execution-step-result pre').text()).toContain('mysql Ver 8.0.45')
    expect(wrapper.find('.execution-step-status').text()).toBe('执行完成')
    expect(wrapper.find('.execution-result-summary').text()).toBe('已完成：检查 MySQL 版本；返回 2 行输出。')
  })

  it('keeps confirmation controls inside the same execution step', async () => {
    const wrapper = mount(ExecutionStep, { props: { preview, actionable: true } })
    expect(wrapper.find('.execution-confirm').exists()).toBe(true)
    await wrapper.findAll('.execution-confirm button')[1].trigger('click')
    expect(wrapper.emitted('confirm')?.[0]).toEqual([true, ''])
  })

  it('disables both confirmation actions and reports submission immediately', async () => {
    const wrapper = mount(ExecutionStep, {
      props: {
        preview,
        actionable: true,
        submitting: true,
        taskStep: {
          type: 'task_step', task_id: 'task-1', step_index: 1, total_steps: 1,
          status: 'pending', content: '等待确认', intent: '检查 MySQL 版本',
          command: 'mysql --version', target: 'dev-01',
        },
      },
    })
    const buttons = wrapper.findAll('.execution-confirm button')

    expect(wrapper.find('.execution-step-status').text()).toBe('提交中')
    expect(wrapper.find('.execution-confirm').attributes('aria-busy')).toBe('true')
    expect(buttons[0].attributes('disabled')).toBeDefined()
    expect(buttons[1].attributes('disabled')).toBeDefined()
    expect(buttons[1].text()).toBe('提交中…')
    await buttons[1].trigger('click')
    expect(wrapper.emitted('confirm')).toBeUndefined()
  })

  it('shows command content from a task step before its preview arrives', () => {
    const wrapper = mount(ExecutionStep, {
      props: {
        taskStep: {
          type: 'task_step', task_id: 'task-1', step_index: 2, total_steps: 4,
          status: 'pending', content: '等待确认', intent: '确认进程已退出',
          command: 'ps aux | grep service.jar', target: 'dev-01',
        },
      },
    })

    expect(wrapper.find('.execution-step-index').text()).toBe('2/4')
    expect(wrapper.find('.execution-step-command').text()).toContain('ps aux | grep service.jar')
    expect(wrapper.find('.execution-step-status').text()).toBe('等待确认')
  })

  it.each(['caution', 'dangerous', 'critical'] as const)('opens risk reasons by default for %s commands', (riskLevel) => {
    const wrapper = mount(ExecutionStep, {
      props: {
        preview: { ...preview, risk_level: riskLevel, risk_reasons: ['会修改服务状态'] },
      },
    })

    expect(wrapper.find('details.execution-step-context').attributes('open')).toBeDefined()
    expect(wrapper.find('details.execution-step-context').text()).toContain('会修改服务状态')
  })

  it('keeps informational reasons collapsed for safe commands', () => {
    const wrapper = mount(ExecutionStep, {
      props: {
        preview: { ...preview, risk_reasons: ['只读取服务状态'] },
      },
    })

    expect(wrapper.find('details.execution-step-context').attributes('open')).toBeUndefined()
  })
})
