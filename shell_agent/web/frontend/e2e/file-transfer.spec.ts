import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const fileName = 'bedcare-mock.jar'
const remotePath = `/tmp/shell-agent-uploads/${fileName}`

async function resetTransfers(request: APIRequestContext) {
  const response = await request.post('/__test__/reset', { data: { scenario: 'file_transfer' } })
  expect(response.ok()).toBeTruthy()
}

async function openTransferDialog(page: Page) {
  await page.goto('/next/#/chat')
  const file = page.locator('.file-row').filter({ hasText: fileName })
  await expect(file).toBeVisible()
  await file.getByRole('button', { name: /传到服务器|上传中|重试传输|传输结果/ }).click()

  const dialog = page.getByRole('dialog', { name: `将 ${fileName} 传到服务器` })
  await expect(dialog).toBeVisible()
  return { dialog, file }
}

async function beginTransfer(page: Page) {
  const { dialog, file } = await openTransferDialog(page)
  await expect(dialog.getByRole('combobox')).toHaveValue('fake-host')
  await expect(dialog.getByText(`fake-host:${remotePath}`, { exact: true })).toBeVisible()
  const submit = dialog.getByRole('button', { name: '确认并上传' })
  await expect(submit).toBeEnabled()
  await submit.click()
  await expect(dialog.getByRole('button', { name: '上传中…' })).toBeDisabled()
  await expect(dialog.getByText('正在通过 SFTP 上传…', { exact: true })).toBeVisible()
  return { dialog, file }
}

test.describe('session file transfer uses an isolated fake backend', () => {
  test.beforeEach(async ({ request }) => {
    await resetTransfers(request)
  })

  test('shows the upload date on each session file card', async ({ page }) => {
    await page.goto('/next/#/chat')

    const file = page.locator('.file-row').filter({ hasText: fileName })
    await expect(file).toBeVisible()
    await expect(file.getByText('2026-07-16 09:00:00', { exact: true })).toBeVisible()
    await expect(file.getByText('· 17.4 MB · 已解析', { exact: true })).toBeVisible()
  })

  test('opens the transfer dialog with the session file and fake target', async ({ page }) => {
    const { dialog } = await openTransferDialog(page)

    await expect(dialog.getByText('会话文件传输', { exact: true })).toBeVisible()
    await expect(dialog.getByText(fileName, { exact: true })).toBeVisible()
    await expect(dialog.getByRole('combobox')).toHaveValue('fake-host')
    await expect(dialog.getByText(`fake-host:${remotePath}`, { exact: true })).toBeVisible()
    await expect(dialog.getByText('不会自动部署、解压或执行该文件', { exact: false })).toBeVisible()
  })

  test('double submit and a repeated request create exactly one transfer execution', async ({ page, request }) => {
    const { dialog } = await openTransferDialog(page)
    const submit = dialog.getByRole('button', { name: '确认并上传' })

    await submit.evaluate((element: HTMLButtonElement) => {
      element.click()
      element.click()
    })

    await expect(dialog.getByRole('button', { name: '上传中…' })).toBeDisabled()
    await expect.poll(async () => {
      const snapshot = await request.get('/__test__/state')
      const data = await snapshot.json()
      return {
        posts: data.transfer_post_count,
        executions: data.transfer_execution_count,
      }
    }).toEqual({ posts: 1, executions: 1 })

    const firstSnapshot = await (await request.get('/__test__/state')).json()
    const requestId = Object.keys(firstSnapshot.transfer_request_ids)[0]
    const firstTransfer = Object.values(firstSnapshot.transfers)[0] as { id: string }
    const repeated = await request.post(`/api/sessions/e2e-session/files/e2e-file/transfers`, {
      data: {
        target: 'fake-host',
        remote_dir: '/tmp/shell-agent-uploads',
        remote_name: fileName,
        overwrite: false,
        request_id: requestId,
      },
    })
    expect(repeated.ok()).toBeTruthy()
    expect((await repeated.json()).transfer.id).toBe(firstTransfer.id)

    const repeatedSnapshot = await (await request.get('/__test__/state')).json()
    expect(repeatedSnapshot.transfer_post_count).toBe(2)
    expect(repeatedSnapshot.transfer_execution_count).toBe(1)
  })

  test('refresh restores a running transfer and later shows the verified remote path', async ({ page, request }) => {
    await beginTransfer(page)

    await page.reload()
    const file = page.locator('.file-row').filter({ hasText: fileName })
    await expect(file.getByRole('button', { name: '上传中' })).toBeVisible()
    await file.getByRole('button', { name: '上传中' }).click()

    const dialog = page.getByRole('dialog', { name: `将 ${fileName} 传到服务器` })
    await expect(dialog.getByText('正在通过 SFTP 上传…', { exact: true })).toBeVisible()
    await expect(dialog.getByRole('button', { name: '上传中…' })).toBeDisabled()

    const completed = await request.post('/__test__/transfer/finish', { data: { outcome: 'success' } })
    expect(completed.ok()).toBeTruthy()

    await expect(dialog.getByText('文件传输完成', { exact: true })).toBeVisible()
    await expect(dialog.getByText(`fake-host:${remotePath}`, { exact: true })).toBeVisible()
    await expect(dialog.getByText('9f74b3a3125b91787ef80d8a26823d278bfb2c86d3cb3aefa6f3fbc09dd63f4a', { exact: true })).toBeVisible()

    const snapshot = await request.get('/__test__/state')
    const data = await snapshot.json()
    expect(data.transfer_execution_count).toBe(1)
  })

  test('failed transfer remains visible and can be retried as a new execution', async ({ page, request }) => {
    const { dialog } = await beginTransfer(page)
    const failed = await request.post('/__test__/transfer/finish', {
      data: { outcome: 'failed', error: 'Permission denied' },
    })
    expect(failed.ok()).toBeTruthy()

    await expect(dialog.getByRole('alert').filter({ hasText: 'Permission denied' })).toBeVisible()
    const retry = dialog.getByRole('button', { name: '重试上传' })
    await expect(retry).toBeEnabled()
    await retry.click()

    await expect(dialog.getByRole('button', { name: '上传中…' })).toBeDisabled()
    await expect.poll(async () => {
      const snapshot = await request.get('/__test__/state')
      return (await snapshot.json()).transfer_execution_count
    }).toBe(2)

    const completed = await request.post('/__test__/transfer/finish', { data: { outcome: 'success' } })
    expect(completed.ok()).toBeTruthy()
    await expect(dialog.getByText('文件传输完成', { exact: true })).toBeVisible()
    await expect(dialog.getByText(`fake-host:${remotePath}`, { exact: true })).toBeVisible()
  })
})
