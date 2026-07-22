import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, http } from '../api/http'
import type { ProfileCandidateRecord } from '../api/protocol'
import { useNotificationsStore } from './notifications'
import { useMemoriesStore } from './memories'

const candidate: ProfileCandidateRecord = {
  id: 'candidate-1',
  service_id: 'mysql',
  service_name: 'MySQL',
  proposed_changes: { version: '8.0.46' },
  before_snapshot: { revision: 1 },
}

describe('memories store candidate conflicts', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('refreshes candidates and returns false after a revision conflict', async () => {
    const store = useMemoriesStore()
    const load = vi.spyOn(store, 'load').mockResolvedValue()
    vi.spyOn(http, 'post').mockRejectedValue(new ApiError(
      '画像候选已过期，候选列表已刷新',
      409,
    ))

    await expect(store.acceptCandidate(candidate)).resolves.toBe(false)

    expect(load).toHaveBeenCalledOnce()
    expect(useNotificationsStore().items.at(-1)).toEqual(expect.objectContaining({
      kind: 'info',
      message: '画像候选已过期，候选列表已刷新',
    }))
  })
})
