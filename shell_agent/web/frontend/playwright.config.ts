import { defineConfig, devices } from '@playwright/test'

const host = '127.0.0.1'
const port = 4178
const baseURL = `http://${host}:${port}`

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.spec.ts',
  fullyParallel: false,
  workers: 1,
  timeout: 20_000,
  expect: { timeout: 5_000 },
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    command: 'python3 e2e/fake_server.py',
    url: `${baseURL}/__test__/health`,
    timeout: 20_000,
    reuseExistingServer: false,
    env: {
      SHELL_AGENT_E2E_HOST: host,
      SHELL_AGENT_E2E_PORT: String(port),
    },
  },
})
