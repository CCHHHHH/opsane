import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const fileName = 'bedcare-mock.jar'

async function resetDeployment(request: APIRequestContext, scenario: string) {
  const response = await request.post('/__test__/reset', { data: { scenario } })
  expect(response.ok()).toBeTruthy()
}

function composer(page: Page) {
  return page.locator('textarea.composer-input')
}

async function openDeploymentFromJar(page: Page) {
  await page.goto('/next/#/chat')
  const file = page.locator('.file-row').filter({ hasText: fileName })
  await expect(file).toBeVisible()
  const deploy = file.getByRole('button', { name: '部署', exact: true })
  await expect(deploy).toBeEnabled()
  await deploy.click()
  const card = page.locator('.deployment-run-card')
  await expect(card).toBeVisible()
  return card
}

test.describe('single-host Java JAR deployment runbook uses only the fake backend', () => {
  test('creates a frozen plan, suppresses double confirm, restores running state, and unlocks on completion', async ({ page, request }) => {
    await resetDeployment(request, 'deployment_idle')
    const card = await openDeploymentFromJar(page)

    await expect(card.getByText('部署 Runbook', { exact: true })).toBeVisible()
    await expect(card.getByText('bedcare-mock', { exact: true })).toBeVisible()
    await expect(card.getByText('等待确认方案', { exact: true })).toBeVisible()
    await expect(card.getByText('部署前检查已通过', { exact: true })).toBeVisible()
    await expect(card.locator('.deployment-meta')).toContainText('目标fake-host')
    await expect(card.locator('.deployment-meta')).toContainText(`制品${fileName}`)
    await expect(card.getByText(/方案校验 e2e-frozen/)).toBeVisible()
    await expect(composer(page)).toBeDisabled()

    const createdState = await (await request.get('/__test__/state')).json()
    expect(createdState.deployment_create_count).toBe(1)
    expect(createdState.deployment_execution_count).toBe(0)

    const confirm = card.getByRole('button', { name: '确认并开始部署' })
    await confirm.evaluate((element: HTMLButtonElement) => {
      element.click()
      element.click()
    })

    await expect(card.getByRole('button', { name: '确认中…' })).toBeDisabled()
    await expect.poll(async () => {
      const snapshot = await request.get('/__test__/state')
      const data = await snapshot.json()
      return {
        confirms: data.deployment_confirm_count,
        executions: data.deployment_execution_count,
      }
    }).toEqual({ confirms: 1, executions: 1 })
    await expect(card.getByText('正在上传制品', { exact: true })).toBeVisible()

    await page.reload()

    const restoredCard = page.locator('.deployment-run-card')
    await expect(restoredCard.getByText('正在上传制品', { exact: true })).toBeVisible()
    await expect(page.getByText('部署 Runbook 正在占用当前会话，请在部署卡片中处理', { exact: true })).toBeVisible()
    await expect(composer(page)).toBeDisabled()
    await expect(page.getByRole('button', { name: '发送' })).toBeDisabled()

    const completed = await request.post('/__test__/deployment/advance', {
      data: { status: 'completed' },
    })
    expect(completed.ok()).toBeTruthy()

    await expect(restoredCard.getByText('部署完成', { exact: true })).toBeVisible()
    await expect(restoredCard.getByText('制品已替换并通过全部部署后验证。', { exact: true })).toBeVisible()
    await expect(composer(page)).toBeEnabled()
    await expect(composer(page)).toHaveAttribute('placeholder', '描述你想完成的任务…')
  })

  test('rollback confirmation is immediately disabled and submitted exactly once', async ({ page, request }) => {
    await resetDeployment(request, 'deployment_rollback_required')
    await page.goto('/next/#/chat')

    const card = page.locator('.deployment-run-card')
    await expect(card.locator('.deployment-status')).toHaveText('需要回滚')
    await expect(card.getByText('部署已经改变远端服务且验证未通过。请确认恢复部署前制品并重新验证服务。', { exact: true })).toBeVisible()
    await expect(card.getByText('回滚步骤', { exact: true })).toBeVisible()
    await expect(card.getByText('恢复部署前制品', { exact: true })).toBeVisible()
    await expect(composer(page)).toBeDisabled()

    const rollback = card.getByRole('button', { name: '确认回滚到部署前版本' })
    await rollback.evaluate((element: HTMLButtonElement) => {
      element.click()
      element.click()
    })

    await expect(card.getByRole('button', { name: '提交中…' })).toBeDisabled()
    await expect.poll(async () => {
      const snapshot = await request.get('/__test__/state')
      return (await snapshot.json()).deployment_rollback_count
    }).toBe(1)
    await expect(card.getByText('正在回滚', { exact: true })).toBeVisible()
    await expect(composer(page)).toBeDisabled()
  })

  test('unknown remote state requires manual verification and keeps the conversation locked', async ({ page, request }) => {
    await resetDeployment(request, 'deployment_unknown')
    await page.goto('/next/#/chat')

    const card = page.locator('.deployment-run-card')
    await expect(card.locator('.deployment-status')).toHaveText('远端状态未知')
    await expect(card.getByText('无法确认远端操作是否完成。系统不会盲目重试，请先核对服务、制品和进程状态。', { exact: true })).toBeVisible()
    await expect(composer(page)).toBeDisabled()
    await expect(page.getByRole('button', { name: '发送' })).toBeDisabled()
    await expect(card.locator('.deployment-actions')).toHaveCount(0)

    const snapshot = await request.get('/__test__/state')
    const data = await snapshot.json()
    expect(data.deployment_execution_count).toBe(0)
    expect(data.deployment_rollback_count).toBe(0)
  })
})
