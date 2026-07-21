import { expect, test, type Page, type Route } from '@playwright/test'

const sessionFilesUrl = '**/api/sessions/e2e-session/files'

async function resetIdle(page: Page) {
  const response = await page.request.post('/__test__/reset', { data: { scenario: 'idle' } })
  expect(response.ok()).toBeTruthy()
}

function uploadedRecord(name: string) {
  const extension = name.includes('.') ? name.slice(name.lastIndexOf('.')) : ''
  return {
    id: `uploaded-${name}`,
    session_id: 'e2e-session',
    name,
    media_type: 'text/plain',
    extension,
    kind: 'document',
    preview_type: 'text',
    size: 12,
    sha256: 'e2e-upload-sha256',
    parse_status: 'ready',
    parse_error: '',
    metadata: {},
    created_at: '2026-07-17T08:00:00',
    preview_url: `/api/session-files/uploaded-${name}/preview`,
    content_url: `/api/session-files/uploaded-${name}/content`,
    download_url: `/api/session-files/uploaded-${name}/download`,
  }
}

async function interceptUpload(page: Page, name: string) {
  const bodies: string[] = []
  await page.route(sessionFilesUrl, async (route: Route) => {
    if (route.request().method() !== 'POST') {
      await route.continue()
      return
    }
    bodies.push(route.request().postDataBuffer()?.toString('utf8') ?? '')
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ files: [uploadedRecord(name)] }),
    })
  })
  return bodies
}

test.describe('composer accepts pasted and dropped session files', () => {
  test.beforeEach(async ({ page }) => {
    await resetIdle(page)
  })

  test('Ctrl/Cmd+V uploads a clipboard file without submitting a chat message', async ({ page }) => {
    const name = 'clipboard-note.txt'
    const bodies = await interceptUpload(page, name)
    await page.goto('/next/#/chat')

    const input = page.locator('textarea.composer-input')
    await expect(input).toBeEnabled()
    await input.evaluate((element, fileName) => {
      const transfer = new DataTransfer()
      transfer.items.add(new File(['from clipboard'], fileName, { type: 'text/plain' }))
      const event = new Event('paste', { bubbles: true, cancelable: true })
      Object.defineProperty(event, 'clipboardData', { value: transfer })
      element.dispatchEvent(event)
    }, name)

    await expect(page.getByText('已上传 1 个文件', { exact: true })).toBeVisible()
    expect(bodies).toHaveLength(1)
    expect(bodies[0]).toContain(`filename="${name}"`)
    await expect(input).toHaveValue('')
  })

  test('ordinary pasted text is not intercepted as a file upload', async ({ page }) => {
    const bodies = await interceptUpload(page, 'unused.txt')
    await page.goto('/next/#/chat')

    const input = page.locator('textarea.composer-input')
    await expect(input).toBeEnabled()
    const defaultAllowed = await input.evaluate((element) => {
      const transfer = new DataTransfer()
      transfer.setData('text/plain', 'keep this text')
      const event = new Event('paste', { bubbles: true, cancelable: true })
      Object.defineProperty(event, 'clipboardData', { value: transfer })
      return element.dispatchEvent(event)
    })

    expect(defaultAllowed).toBe(true)
    expect(bodies).toHaveLength(0)
  })

  test('dragging over the composer shows a drop target and dropping uploads once', async ({ page }) => {
    const name = 'dragged-service.log'
    const bodies = await interceptUpload(page, name)
    await page.goto('/next/#/chat')

    const composer = page.locator('form.composer')
    await expect(page.locator('textarea.composer-input')).toBeEnabled()
    await composer.evaluate((element, fileName) => {
      const transfer = new DataTransfer()
      transfer.items.add(new File(['from drag'], fileName, { type: 'text/plain' }))
      ;(window as typeof window & { __composerTransfer?: DataTransfer }).__composerTransfer = transfer
      element.dispatchEvent(new DragEvent('dragenter', {
        bubbles: true,
        cancelable: true,
        dataTransfer: transfer,
      }))
    }, name)

    await expect(page.getByText('释放文件以上传到当前会话', { exact: true })).toBeVisible()

    await composer.evaluate((element) => {
      const transfer = (window as typeof window & { __composerTransfer?: DataTransfer }).__composerTransfer
      element.dispatchEvent(new DragEvent('dragleave', {
        bubbles: true,
        cancelable: true,
        dataTransfer: transfer,
      }))
    })
    await expect(page.getByText('释放文件以上传到当前会话', { exact: true })).toBeHidden()

    await composer.evaluate((element) => {
      const transfer = (window as typeof window & { __composerTransfer?: DataTransfer }).__composerTransfer
      element.dispatchEvent(new DragEvent('dragenter', {
        bubbles: true,
        cancelable: true,
        dataTransfer: transfer,
      }))
    })
    await expect(page.getByText('释放文件以上传到当前会话', { exact: true })).toBeVisible()

    await composer.evaluate((element) => {
      const transfer = (window as typeof window & { __composerTransfer?: DataTransfer }).__composerTransfer
      element.dispatchEvent(new DragEvent('drop', {
        bubbles: true,
        cancelable: true,
        dataTransfer: transfer,
      }))
    })

    await expect(page.getByText('释放文件以上传到当前会话', { exact: true })).toBeHidden()
    await expect(page.getByText('已上传 1 个文件', { exact: true })).toBeVisible()
    expect(bodies).toHaveLength(1)
    expect(bodies[0]).toContain(`filename="${name}"`)
  })

  test('a busy conversation rejects a dropped file without browser navigation or upload', async ({ page }) => {
    const response = await page.request.post('/__test__/reset', { data: { scenario: 'active' } })
    expect(response.ok()).toBeTruthy()
    const bodies = await interceptUpload(page, 'blocked.txt')
    await page.goto('/next/#/chat')

    const originalUrl = page.url()
    const composer = page.locator('form.composer')
    await expect(page.locator('textarea.composer-input')).toBeDisabled()
    const dropPrevented = await composer.evaluate((element) => {
      const transfer = new DataTransfer()
      transfer.items.add(new File(['blocked'], 'blocked.txt', { type: 'text/plain' }))
      const event = new DragEvent('drop', {
        bubbles: true,
        cancelable: true,
        dataTransfer: transfer,
      })
      return !element.dispatchEvent(event)
    })

    expect(dropPrevented).toBe(true)
    await expect(page.getByText('当前会话暂时无法接收文件，请等待当前任务完成', { exact: true })).toBeVisible()
    expect(bodies).toHaveLength(0)
    expect(page.url()).toBe(originalUrl)
  })
})
