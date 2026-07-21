import { describe, expect, it } from 'vitest'

import { formatCompactDateTime } from './dateTime'

describe('compact date time formatting', () => {
  it('formats a local ISO timestamp down to seconds', () => {
    expect(formatCompactDateTime('2026-07-17T09:05:07')).toBe('2026-07-17 09:05:07')
  })

  it('uses a clear fallback for invalid timestamps', () => {
    expect(formatCompactDateTime('not-a-date')).toBe('时间未知')
  })
})
