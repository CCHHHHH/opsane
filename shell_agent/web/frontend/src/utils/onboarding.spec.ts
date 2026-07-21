import { beforeEach, describe, expect, it } from 'vitest'

import {
  completeOnboarding,
  ONBOARDING_STORAGE_KEY,
  ONBOARDING_VERSION,
  resetOnboarding,
  shouldShowOnboarding,
} from './onboarding'

describe('first-use onboarding preference', () => {
  beforeEach(() => localStorage.clear())

  it('shows once, then stays completed across reloads', () => {
    expect(shouldShowOnboarding()).toBe(true)

    completeOnboarding()

    expect(localStorage.getItem(ONBOARDING_STORAGE_KEY)).toBe(ONBOARDING_VERSION)
    expect(shouldShowOnboarding()).toBe(false)
  })

  it('can be reset for a future first-use run', () => {
    completeOnboarding()
    resetOnboarding()

    expect(shouldShowOnboarding()).toBe(true)
  })

  it('fails open when browser storage is unavailable', () => {
    const blockedStorage = {
      getItem: () => { throw new Error('blocked') },
      setItem: () => { throw new Error('blocked') },
      removeItem: () => { throw new Error('blocked') },
    }

    expect(shouldShowOnboarding(blockedStorage)).toBe(true)
    expect(() => completeOnboarding(blockedStorage)).not.toThrow()
  })
})
