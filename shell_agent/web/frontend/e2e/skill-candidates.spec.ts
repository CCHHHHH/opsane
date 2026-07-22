import { expect, test } from '@playwright/test'


test.describe('history-derived Skill candidate review', () => {
  test.beforeEach(async ({ request }) => {
    await request.post('/__test__/reset', { data: { scenario: 'idle' } })
  })

  test('previews without execution and publishes only as disabled', async ({ page }) => {
    await page.goto('/next/#/config')
    await page.getByRole('button', { name: 'Skills' }).click()

    const candidate = page.locator('.candidate-card')
    await expect(candidate).toContainText('learned_uptime_e2e')
    await candidate.locator('summary').click()
    await candidate.getByRole('button', { name: '安全预览' }).click()
    await expect(candidate).toContainText('安全预览（不会执行）')
    await expect(candidate).toContainText("ssh fake-host 'uptime'")

    page.once('dialog', (dialog) => dialog.accept())
    await candidate.getByRole('button', { name: '批准并创建停用 Skill' }).click()

    await expect(page.getByText('暂无待审核候选')).toBeVisible()
    await expect(page.getByRole('cell', { name: 'learned_uptime_e2e' })).toBeVisible()
    await expect(page.getByRole('cell', { name: '停用' })).toBeVisible()
  })

  test('semantic scan is explicit and sends only a bounded scan request', async ({ page, request }) => {
    await page.goto('/next/#/config')
    await page.getByRole('button', { name: 'Skills' }).click()

    await page.getByRole('button', { name: '语义扫描最近 30 天' }).click()
    await expect(page.getByText(/语义扫描完成：新增 1 个 Skill 候选/)).toBeVisible()

    const fixture = await request.get('/__test__/state')
    const state = await fixture.json()
    expect(state.last_skill_scan).toEqual({
      days: 30,
      min_occurrences: 3,
      semantic: true,
    })
  })
})
