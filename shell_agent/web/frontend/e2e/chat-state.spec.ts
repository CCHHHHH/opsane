import { expect, test, type APIRequestContext } from '@playwright/test'

async function resetScenario(request: APIRequestContext, scenario: string) {
  const response = await request.post('/__test__/reset', { data: { scenario } })
  expect(response.ok()).toBeTruthy()
}

test.describe('chat task state across user actions and refreshes', () => {
  test('places command confirmation actions on the left', async ({ page, request }) => {
    await resetScenario(request, 'waiting_confirm')
    await page.goto('/next/#/chat')

    const actions = page.locator('.execution-confirm-actions')
    const reject = actions.getByRole('button', { name: '拒绝' })
    await expect(actions).toBeVisible()
    await expect(reject).toBeVisible()

    const actionsBox = await actions.boundingBox()
    const rejectBox = await reject.boundingBox()
    expect(actionsBox).not.toBeNull()
    expect(rejectBox).not.toBeNull()
    expect(Math.abs((rejectBox?.x ?? 0) - (actionsBox?.x ?? 0))).toBeLessThanOrEqual(2)
  })

  test('double confirmation submits once and disables the action immediately', async ({ page, request }) => {
    await resetScenario(request, 'waiting_confirm')
    await page.goto('/next/#/chat')

    const confirm = page.getByRole('button', { name: '确认执行' })
    await expect(confirm).toBeVisible()
    await confirm.evaluate((element: HTMLButtonElement) => {
      element.click()
      element.click()
    })

    const submitting = page.getByRole('button', { name: '提交中' })
    await expect(submitting).toBeDisabled()
    await page.waitForTimeout(700)

    const snapshot = await request.get('/__test__/state')
    expect((await snapshot.json()).confirm_count).toBe(1)
  })

  test('refresh restores a waiting confirmation and keeps it actionable', async ({ page, request }) => {
    await resetScenario(request, 'waiting_confirm')
    await page.goto('/next/#/chat')

    await expect(page.getByText('等待确认', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: '确认执行' })).toBeEnabled()

    await page.reload()

    await expect(page.getByText('等待确认', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: '确认执行' })).toBeEnabled()
    await expect(page.getByText('systemctl restart demo-service', { exact: true })).toBeVisible()
  })

  test('refresh restores an active task, follows later progress, and unlocks without another refresh', async ({ page, request }) => {
    await resetScenario(request, 'active')
    await page.goto('/next/#/chat')

    const input = page.getByPlaceholder('描述你想完成的任务…')
    await expect(page.getByText('正在判断是否需要继续下一步', { exact: true })).toBeVisible()
    await expect(input).toBeDisabled()

    await page.reload()

    await expect(page.getByText('正在判断是否需要继续下一步', { exact: true })).toBeVisible()
    await expect(input).toBeDisabled()
    await expect(page.getByRole('button', { name: '发送' })).toBeDisabled()

    const completed = await request.post('/__test__/complete')
    expect(completed.ok()).toBeTruthy()
    expect((await completed.json()).delivered).toBeGreaterThan(0)

    await expect(page.getByText('演示任务已完成', { exact: true })).toBeVisible()
    await expect(input).toBeEnabled()
    await expect(page.getByRole('button', { name: '发送' })).toBeDisabled()
  })
})
