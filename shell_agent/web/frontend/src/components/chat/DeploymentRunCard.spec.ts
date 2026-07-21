import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { DeploymentRunRecord } from '../../api/deployments'
import DeploymentRunCard from './DeploymentRunCard.vue'

function fixture(status: DeploymentRunRecord['status'] = 'waiting_plan_confirm'): DeploymentRunRecord {
  const definitions = [
    { id: 'precheck', name: '检查目标服务器', phase: 'precheck', action: 'precheck_host', risk_level: 'safe' },
    { id: 'upload', name: '上传制品到隔离暂存目录', phase: 'execute', action: 'stage_upload', risk_level: 'caution' },
    { id: 'switch', name: '原子替换服务制品', phase: 'execute', action: 'switch_artifact', risk_level: 'dangerous', mutates_live: true },
    { id: 'health', name: '检查服务健康状态', phase: 'postcheck', action: 'postcheck_health', risk_level: 'safe' },
    { id: 'restore', name: '恢复部署前制品', phase: 'rollback', action: 'rollback_restore', risk_level: 'dangerous', mutates_live: true },
  ]
  return {
    id: 'deprun-1', session_id: 'session-1', service_id: 'bedcare-mock', target: 'dev-01',
    environment: 'dev', status, plan_hash: '1234567890abcdef'.repeat(4), error: '',
    result_summary: status === 'completed' ? '制品已替换，健康检查通过。' : '',
    plan: {
      service: { service_id: 'bedcare-mock', service_name: 'bedcare-mock', target: 'dev-01', environment: 'dev' },
      artifact: { file_id: 'file-1', name: 'bedcare-mock.jar', size: 18 * 1024 * 1024, sha256: 'a'.repeat(64) },
      steps: definitions,
    },
    steps: definitions.map((step, index) => ({
      ...step, step_id: step.id, step_index: index, status: index === 0 ? 'success' : 'pending',
    })),
    events: [], updated_at: '2026-07-16T10:00:00Z',
  }
}

describe('DeploymentRunCard', () => {
  it('shows the frozen plan, target artifact, and every primary deployment step', () => {
    const wrapper = mount(DeploymentRunCard, { props: { run: fixture() } })

    expect(wrapper.find('.deployment-title h3').text()).toBe('bedcare-mock')
    expect(wrapper.find('.deployment-meta').text()).toContain('dev-01')
    expect(wrapper.find('.deployment-meta').text()).toContain('bedcare-mock.jar')
    expect(wrapper.findAll('.deployment-steps').at(0)?.findAll('li')).toHaveLength(4)
    expect(wrapper.text()).toContain('原子替换服务制品')
    expect(wrapper.text()).toContain('方案校验 1234567890ab…')
  })

  it('locks confirmation locally on the first click so a double click emits only once', async () => {
    const wrapper = mount(DeploymentRunCard, { props: { run: fixture() } })
    const button = wrapper.get('[data-action="confirm"]')

    await button.trigger('click')
    await button.trigger('click')

    expect(wrapper.emitted('confirm')).toHaveLength(1)
    expect(button.attributes('disabled')).toBeDefined()
    expect(button.text()).toBe('确认中…')
    expect(wrapper.get('[data-action="cancel"]').attributes('disabled')).toBeDefined()
  })

  it('offers the explicit rollback flow after a mutation failure', async () => {
    const rollback = fixture('rollback_required')
    rollback.error = '健康检查未通过'
    const wrapper = mount(DeploymentRunCard, { props: { run: rollback } })

    expect(wrapper.text()).toContain('部署已经改变远端服务')
    expect(wrapper.text()).toContain('恢复部署前制品')
    await wrapper.get('[data-action="rollback"]').trigger('click')
    expect(wrapper.emitted('rollbackConfirm')).toHaveLength(1)
  })

  it.each([
    ['unknown', '无法确认远端操作是否完成'],
    ['manual_intervention', '自动恢复未能确认服务安全'],
  ] as const)('makes %s an explicit manual safety state', (status, message) => {
    const wrapper = mount(DeploymentRunCard, { props: { run: fixture(status) } })

    expect(wrapper.get('.deployment-critical').text()).toContain(message)
    expect(wrapper.find('.deployment-actions').exists()).toBe(false)
  })

  it('renders a verified terminal result without leaving action controls behind', () => {
    const completed = fixture('completed')
    completed.steps = completed.steps.map((step) => ({ ...step, status: 'success' }))
    const wrapper = mount(DeploymentRunCard, { props: { run: completed } })

    expect(wrapper.get('.deployment-status').text()).toContain('部署完成')
    expect(wrapper.get('.deployment-result').text()).toContain('健康检查通过')
    expect(wrapper.find('.deployment-actions').exists()).toBe(false)
  })
})
