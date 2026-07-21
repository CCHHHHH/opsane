import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { ServerEvent } from '../../api/protocol'
import ChatTimelineItem from './ChatTimelineItem.vue'

function mountEvent(event: ServerEvent) {
  return mount(ChatTimelineItem, { props: { event } })
}

describe('ChatTimelineItem Markdown boundary', () => {
  it('renders sanitized Markdown for Agent messages', () => {
    const wrapper = mountEvent({
      type: 'agent',
      content: '## 结果\n\n**正常** <img src=x onerror="alert(1)">',
    })

    expect(wrapper.find('h2').text()).toBe('结果')
    expect(wrapper.find('strong').text()).toBe('正常')
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.html()).not.toContain('onerror')
  })

  it.each(['user_message', 'system'] as const)('keeps %s content escaped as text', (type) => {
    const wrapper = mountEvent({ type, content: '**literal** <strong>not markup</strong>' })

    expect(wrapper.find('strong').exists()).toBe(false)
    expect(wrapper.text()).toContain('**literal** <strong>not markup</strong>')
  })

  it('renders sanitized Markdown for the final task summary', () => {
    const wrapper = mountEvent({
      type: 'task_step',
      status: 'complete',
      intent: '任务完成',
      content: '## 最终结论\n\n**正常**\n\n| 目标 | 状态 |\n| --- | --- |\n| dev-01 | 正常 |\n\n<img src=x onerror="alert(1)">',
    })

    expect(wrapper.find('.task-summary-card').exists()).toBe(true)
    expect(wrapper.find('h2').text()).toBe('最终结论')
    expect(wrapper.find('.task-summary-body strong').text()).toBe('正常')
    expect(wrapper.find('.task-summary-body table').exists()).toBe(true)
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.html()).not.toContain('onerror')
  })

  it('keeps non-final task step content escaped as text', () => {
    const wrapper = mountEvent({
      type: 'task_step',
      status: 'success',
      intent: '检查服务',
      content: '**执行完成** <script>alert(1)</script>',
    })

    expect(wrapper.find('.task-summary-card').exists()).toBe(false)
    expect(wrapper.find('.task-step small').text()).toContain('**执行完成** <script>alert(1)</script>')
    expect(wrapper.find('.task-step small strong').exists()).toBe(false)
    expect(wrapper.find('script').exists()).toBe(false)
  })
})

describe('ChatTimelineItem execution result details', () => {
  it('shows the executed command, target, directory, and output together', () => {
    const wrapper = mountEvent({
      type: 'execution_result',
      success: true,
      exit_code: 0,
      command: 'ssh dev-01 uptime',
      target: 'dev-01',
      cwd: '/data/app',
      output: 'up 10 days',
    })

    expect(wrapper.find('.result-command').text()).toBe('ssh dev-01 uptime')
    expect(wrapper.find('.result-output').text()).toBe('up 10 days')
    expect(wrapper.find('.result-meta').text()).toContain('dev-01')
    expect(wrapper.find('.result-meta').text()).toContain('/data/app')
  })

  it('keeps old execution results readable when command metadata is absent', () => {
    const wrapper = mountEvent({
      type: 'execution_result',
      success: true,
      exit_code: 0,
      output: 'legacy output',
    })

    expect(wrapper.find('.result-command').exists()).toBe(false)
    expect(wrapper.find('.result-output').text()).toBe('legacy output')
  })
})

describe('ChatTimelineItem file transfer history', () => {
  it('renders a compact artifact upload result instead of an unknown event', () => {
    const wrapper = mountEvent({
      type: 'artifact_upload',
      content: '制品已上传',
      artifact: {
        file_id: 'file-1', file_name: 'bedcare-mock.jar', target: 'dev-01',
        remote_path: '/data/app/bedcare-mock.jar', remote_size: 1024,
        remote_sha256: 'abcdef1234567890', status: 'success',
      },
    })

    expect(wrapper.find('.artifact-upload-event').exists()).toBe(true)
    expect(wrapper.text()).toContain('文件已传输')
    expect(wrapper.text()).toContain('bedcare-mock.jar')
    expect(wrapper.text()).toContain('dev-01:/data/app/bedcare-mock.jar')
    expect(wrapper.text()).toContain('abcdef123456')
    expect(wrapper.text()).not.toContain('收到事件')
  })

  it('renders a mandatory conversational transfer confirmation and disables both actions while submitting', async () => {
    const event: ServerEvent = {
      type: 'file_transfer_preview', session_id: 'session-1', turn_id: 'turn-1', channel: 'chat',
      requires_confirmation: true, confirm_mode: 'interactive',
      transfer: {
        id: 'xfer-1', file_name: 'release.jar', target: 'dev-01',
        remote_path: '/tmp/releases/release.jar', size: 2048, sha256: 'abcdef1234567890',
        status: 'waiting_confirm', overwrite: false,
      },
    }
    const wrapper = mount(ChatTimelineItem, { props: { event, actionable: true, submitting: true } })

    expect(wrapper.find('.file-transfer-preview-card').exists()).toBe(true)
    expect(wrapper.text()).toContain('release.jar')
    expect(wrapper.text()).toContain('dev-01:/tmp/releases/release.jar')
    expect(wrapper.text()).toContain('完全访问模式也必须由你确认')
    const buttons = wrapper.findAll('.structured-actions button')
    expect(buttons).toHaveLength(2)
    expect(buttons.every((button) => button.attributes('disabled') !== undefined)).toBe(true)
    expect(buttons.map((button) => button.text())).toEqual(['提交中…', '提交中…'])
  })

  it.each([
    ['executing', '上传中', 'badge-warning'],
    ['completed', '已完成', 'badge-success'],
    ['failed', '失败', 'badge-danger'],
    ['canceled', '已取消', 'badge-danger'],
  ])('uses authoritative %s state instead of the frozen preview state', (transferStatus, label, badge) => {
    const event: ServerEvent = {
      type: 'file_transfer_preview', session_id: 'session-1', turn_id: 'turn-1', channel: 'chat',
      requires_confirmation: true, confirm_mode: 'interactive',
      transfer: {
        id: 'xfer-1', file_name: 'release.jar', target: 'dev-01',
        remote_path: '/tmp/releases/release.jar', status: 'waiting_confirm',
      },
    }
    const wrapper = mount(ChatTimelineItem, { props: { event, transferStatus } })
    const status = wrapper.find('.structured-header .badge')

    expect(status.text()).toBe(label)
    expect(status.classes()).toContain(badge)
  })

  it('shows a failed conversational file transfer clearly', () => {
    const wrapper = mountEvent({
      type: 'artifact_upload', content: 'Permission denied',
      artifact: {
        id: 'xfer-failed', file_name: 'release.jar', target: 'dev-01',
        remote_path: '/srv/release.jar', status: 'failed', error: 'Permission denied',
      },
    })

    expect(wrapper.find('.artifact-upload-event.failed').exists()).toBe(true)
    expect(wrapper.text()).toContain('文件传输失败')
    expect(wrapper.text()).toContain('Permission denied')
  })
})

describe('ChatTimelineItem operation plan status', () => {
  const plan = {
    type: 'operation_plan' as const,
    channel: 'chat' as const,
    plan_id: 'plan-1', active: true, intent: '重启服务', title: '服务重启方案',
    goal: '安全重启服务', recommended_approach: '使用服务脚本', impact: [], risks: [],
    rollback: [], verification: [], steps: [],
  }

  it('shows confirmed after an accepted plan is no longer actionable', () => {
    const wrapper = mount(ChatTimelineItem, { props: { event: plan, planStatus: 'confirmed' } })
    expect(wrapper.find('.structured-header .badge').text()).toBe('已确认')
    expect(wrapper.find('.structured-header .badge').classes()).toContain('badge-success')
  })

  it('shows waiting only while the plan remains actionable', () => {
    const wrapper = mount(ChatTimelineItem, { props: { event: plan, actionable: true, planStatus: 'waiting' } })
    expect(wrapper.find('.structured-header .badge').text()).toBe('等待确认')
  })
})
