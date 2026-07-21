import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useNotificationsStore } from './notifications'

describe('notifications store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
  })

  afterEach(() => vi.useRealTimers())

  it('shows typed notifications and removes them automatically', () => {
    const notifications = useNotificationsStore()
    notifications.success('保存成功')
    notifications.error('保存失败')

    expect(notifications.items.map((item) => item.kind)).toEqual(['success', 'error'])
    vi.advanceTimersByTime(5000)
    expect(notifications.items).toHaveLength(0)
  })
})
