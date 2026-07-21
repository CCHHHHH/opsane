export const ONBOARDING_STORAGE_KEY = 'opsane:onboarding:v1'
export const ONBOARDING_VERSION = '1'

type OnboardingStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

function browserStorage(storage?: OnboardingStorage | null): OnboardingStorage | null {
  if (storage !== undefined) return storage
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

export function shouldShowOnboarding(storage?: OnboardingStorage | null): boolean {
  try {
    return browserStorage(storage)?.getItem(ONBOARDING_STORAGE_KEY) !== ONBOARDING_VERSION
  } catch {
    return true
  }
}

export function completeOnboarding(storage?: OnboardingStorage | null): void {
  try {
    browserStorage(storage)?.setItem(ONBOARDING_STORAGE_KEY, ONBOARDING_VERSION)
  } catch {
    // Storage can be unavailable in private browsing or restricted webviews.
  }
}

export function resetOnboarding(storage?: OnboardingStorage | null): void {
  try {
    browserStorage(storage)?.removeItem(ONBOARDING_STORAGE_KEY)
  } catch {
    // A manual restart still works for the current page without persistence.
  }
}
