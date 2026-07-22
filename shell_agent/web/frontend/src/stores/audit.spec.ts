import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { http } from '../api/http'
import { useAuditStore } from './audit'

describe('audit store pagination', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('loads a server page and applies authoritative pagination', async () => {
    const store = useAuditStore()
    store.target = 'dev-01'
    store.page = 2
    const get = vi.spyOn(http, 'get').mockResolvedValue({
      records: [{ id: 'audit-21', target: 'dev-01', command: 'uptime' }],
      pagination: { page: 2, page_size: 20, total: 41, total_pages: 3 },
    })

    await store.load()

    expect(get).toHaveBeenCalledWith('/api/audit', {
      target: 'dev-01', page: 2, page_size: 20,
    })
    expect(store.records).toHaveLength(1)
    expect({ page: store.page, total: store.total, totalPages: store.totalPages }).toEqual({
      page: 2, total: 41, totalPages: 3,
    })
  })

  it('returns to page one when a new target search is submitted', async () => {
    const store = useAuditStore()
    store.page = 4
    const load = vi.spyOn(store, 'load').mockResolvedValue()

    await store.search()

    expect(store.page).toBe(1)
    expect(load).toHaveBeenCalledOnce()
  })
})
