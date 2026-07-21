import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const fileName = 'bedcare-mock.jar'
const remotePath = '/tmp/shell-agent-uploads/bedcare-mock.jar'

async function resetScenario(request: APIRequestContext, scenario: string) {
  const response = await request.post('/__test__/reset', { data: { scenario } })
  expect(response.ok()).toBeTruthy()
}

async function openWaitingPreview(page: Page) {
  await page.goto('/next/#/chat')
  const card = page.locator('.file-transfer-preview-card')
  await expect(card).toBeVisible()
  await expect(card).toContainText(fileName)
  await expect(card).toContainText(`fake-host:${remotePath}`)
  return card
}

async function confirmUpload(page: Page) {
  const confirm = page.getByRole('button', { name: '确认上传' })
  await expect(confirm).toBeEnabled()
  await confirm.click()
  await expect(page.getByRole('button', { name: '提交中…' }).first()).toBeDisabled()
}

test.describe('conversational file transfer uses only the isolated fake backend', () => {
  test('a full-access conversation still creates a mandatory preview and double confirm executes once', async ({ page, request }) => {
    await resetScenario(request, 'conversation_transfer_idle')
    await page.goto('/next/#/chat')

    const input = page.locator('textarea.composer-input')
    await expect(input).toBeEnabled()
    await page.getByRole('combobox', { name: '权限模式' }).selectOption('full_access')
    await expect(page.getByText('完全访问：Opsane 当前拥有最大权限', { exact: true })).toBeVisible()
    await input.fill(`把 ${fileName} 上传到 fake-host 的 /tmp/shell-agent-uploads`)
    await page.getByRole('button', { name: '发送' }).click()

    const card = page.locator('.file-transfer-preview-card')
    await expect(card).toBeVisible()
    await expect(card.getByText('等待确认', { exact: true })).toBeVisible()
    await expect(card).toContainText('即使处于完全访问模式也必须由你确认')

    const confirm = page.getByRole('button', { name: '确认上传' })
    await confirm.evaluate((element: HTMLButtonElement) => {
      element.click()
      element.click()
    })
    await expect(page.getByRole('button', { name: '提交中…' }).first()).toBeDisabled()

    await expect.poll(async () => {
      const snapshot = await request.get('/__test__/state')
      const data = await snapshot.json()
      return {
        confirmations: data.conversation_transfer_confirm_count,
        executions: data.conversation_transfer_execution_count,
      }
    }).toEqual({ confirmations: 1, executions: 1 })
  })

  test('refresh restores the exact waiting preview and keeps it actionable', async ({ page, request }) => {
    await resetScenario(request, 'conversation_transfer_waiting')
    const card = await openWaitingPreview(page)
    await expect(card.getByRole('button', { name: '确认上传' })).toBeEnabled()

    await page.reload()

    const restored = page.locator('.file-transfer-preview-card')
    await expect(restored).toBeVisible()
    await expect(restored).toContainText(`fake-host:${remotePath}`)
    await expect(restored.getByRole('button', { name: '确认上传' })).toBeEnabled()
    await expect(page.locator('textarea.composer-input')).toBeDisabled()
  })

  test('reject locks immediately, never starts a transfer, and settles the turn', async ({ page, request }) => {
    await resetScenario(request, 'conversation_transfer_waiting')
    await openWaitingPreview(page)

    await page.getByRole('button', { name: '拒绝' }).click()
    await expect(page.getByRole('button', { name: '提交中…' }).first()).toBeDisabled()
    await expect(page.locator('.file-transfer-preview-card').getByText('已取消', { exact: true })).toBeVisible()
    await expect(page.getByText('文件传输失败', { exact: true })).toHaveCount(0)

    const snapshot = await request.get('/__test__/state')
    const data = await snapshot.json()
    expect(data.conversation_transfer_confirm_count).toBe(1)
    expect(data.conversation_transfer_execution_count).toBe(0)
    await expect(page.locator('textarea.composer-input')).toBeEnabled()
  })

  test('success shows the verified destination and unlocks the conversation', async ({ page, request }) => {
    await resetScenario(request, 'conversation_transfer_waiting')
    await openWaitingPreview(page)
    await confirmUpload(page)
    await expect.poll(async () => (await (await request.get('/__test__/state')).json()).conversation_transfer_execution_count).toBe(1)

    const completed = await request.post('/__test__/conversation-transfer/finish', { data: { outcome: 'success' } })
    expect(completed.ok()).toBeTruthy()
    expect((await completed.json()).delivered).toBeGreaterThan(0)

    await expect(page.getByText('文件已传输', { exact: true })).toBeVisible()
    await expect(page.getByText(`fake-host:${remotePath}`, { exact: true }).last()).toBeVisible()
    await expect(page.locator('textarea.composer-input')).toBeEnabled()
  })

  test('failure remains explicit and unlocks the conversation for recovery', async ({ page, request }) => {
    await resetScenario(request, 'conversation_transfer_waiting')
    await openWaitingPreview(page)
    await confirmUpload(page)
    await expect.poll(async () => (await (await request.get('/__test__/state')).json()).conversation_transfer_execution_count).toBe(1)

    const failed = await request.post('/__test__/conversation-transfer/finish', { data: { outcome: 'failed' } })
    expect(failed.ok()).toBeTruthy()

    await expect(page.getByText('文件传输失败', { exact: true })).toBeVisible()
    await expect(page.getByText('Permission denied', { exact: true }).first()).toBeVisible()
    await expect(page.locator('textarea.composer-input')).toBeEnabled()
  })
})
