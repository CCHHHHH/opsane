import { describe, expect, it } from 'vitest'

import { normalizeDeploymentRun } from './deployments'

describe('deployment API contract normalization', () => {
  it('merges immutable plan definitions with durable step execution state', () => {
    const run = normalizeDeploymentRun({
      run: {
        id: 'deprun-1', session_id: 'session-1', status: 'postcheck_running',
        plan_hash: 'hash-1', target: 'dev-01', environment: 'dev',
        plan_json: {
          runbook_id: 'single_java_jar_deploy', runbook_version: '1.0.0', run_id: 'deprun-1',
          service: { service_id: 'camel', service_name: 'Camel', target: 'dev-01' },
          artifact: { file_id: 'file-1', name: 'camel.jar', size: 1024 },
          steps: [
            { id: 'health', name: '检查服务健康状态', phase: 'postcheck', action: 'postcheck_health', risk_level: 'safe' },
          ],
        },
      },
      steps: [
        { step_id: 'health', step_index: 12, status: 'running', attempt: 1 },
      ],
      events: [{ type: 'status_changed', status: 'postcheck_running' }],
    })

    expect(run.id).toBe('deprun-1')
    expect(run.plan.service.service_name).toBe('Camel')
    expect(run.steps[0]).toMatchObject({
      step_id: 'health', name: '检查服务健康状态', phase: 'postcheck',
      action: 'postcheck_health', status: 'running', attempt: 1,
    })
    expect(run.events).toHaveLength(1)
  })

  it('accepts a direct run response with embedded steps', () => {
    const run = normalizeDeploymentRun({
      id: 'deprun-2', session_id: 'session-1', status: 'completed', plan_hash: 'hash-2',
      profile_snapshot: { service_id: 'camel', service_name: 'Camel' },
      artifact_snapshot: { file_id: 'file-1', name: 'camel.jar' },
      plan_json: { steps: [] },
      steps: [{ step_id: 'done', name: '完成部署', phase: 'execute', action: 'done', risk_level: 'safe', status: 'success' }],
    })

    expect(run.status).toBe('completed')
    expect(run.steps[0].status).toBe('success')
  })
})
