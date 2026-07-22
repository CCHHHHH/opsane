import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { http } from '../api/http'
import { useInventoryStore, type ServerInput } from './inventory'

const server: ServerInput = {
  alias: 'dev-01',
  host: '10.0.0.12',
  port: 22,
  env: 'dev',
  role: 'app',
  ssh_credential: 'dev-01-ssh',
  tags: ['iot'],
}

describe('inventory server connection test', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('uses the non-persisting SSH probe endpoint', async () => {
    const store = useInventoryStore()
    const post = vi.spyOn(http, 'post').mockResolvedValue({
      ok: true,
      message: 'SSH 连接成功（12 ms）',
      latency_ms: 12,
    })

    await expect(store.testServerConnection(server)).resolves.toEqual({
      ok: true,
      message: 'SSH 连接成功（12 ms）',
      latency_ms: 12,
    })

    expect(post).toHaveBeenCalledWith('/api/servers/test-connection', server)
    expect(store.testingServer).toBe(false)
  })

  it('clears the testing state after a failed probe', async () => {
    const store = useInventoryStore()
    vi.spyOn(http, 'post').mockRejectedValue(new Error('认证失败'))

    await expect(store.testServerConnection(server)).rejects.toThrow('认证失败')
    expect(store.testingServer).toBe(false)
  })
})
