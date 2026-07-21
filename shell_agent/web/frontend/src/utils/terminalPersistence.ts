import type { ServerEvent } from '../api/protocol'

export const MAX_TERMINAL_HISTORY = 100
export const MAX_TERMINAL_SCROLLBACK = 200
const MAX_EVENT_TEXT = 20_000
const STORAGE_PREFIX = 'shell-agent:terminal:v1:'

export interface TerminalSnapshot {
  cwd: string
  history: string[]
  events: ServerEvent[]
}

function storageKey(target: string): string {
  return `${STORAGE_PREFIX}${encodeURIComponent(target || '__default__')}`
}

function getStorage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage
  } catch {
    return null
  }
}

function sanitizeEvent(event: ServerEvent): ServerEvent {
  const sanitized = { ...event }
  for (const key of ['output', 'content', 'command']) {
    const value = sanitized[key]
    if (typeof value === 'string' && value.length > MAX_EVENT_TEXT) {
      sanitized[key] = `${value.slice(0, MAX_EVENT_TEXT)}\n…（本地滚屏已截断）`
    }
  }
  return sanitized
}

export function loadTerminalSnapshot(target: string): TerminalSnapshot | null {
  const storage = getStorage()
  if (!storage) return null
  try {
    const raw = storage.getItem(storageKey(target))
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<TerminalSnapshot>
    return {
      cwd: typeof parsed.cwd === 'string' ? parsed.cwd : '',
      history: Array.isArray(parsed.history) ? parsed.history.filter((item): item is string => typeof item === 'string').slice(0, MAX_TERMINAL_HISTORY) : [],
      events: Array.isArray(parsed.events) ? parsed.events.filter((event): event is ServerEvent => Boolean(event && typeof event === 'object' && typeof event.type === 'string')).slice(-MAX_TERMINAL_SCROLLBACK) : [],
    }
  } catch {
    storage.removeItem(storageKey(target))
    return null
  }
}

export function saveTerminalSnapshot(target: string, snapshot: TerminalSnapshot): void {
  const storage = getStorage()
  if (!storage) return
  const payload: TerminalSnapshot = {
    cwd: snapshot.cwd,
    history: snapshot.history.slice(0, MAX_TERMINAL_HISTORY),
    events: snapshot.events.slice(-MAX_TERMINAL_SCROLLBACK).map(sanitizeEvent),
  }
  try {
    storage.setItem(storageKey(target), JSON.stringify(payload))
  } catch {
    // Local storage may be disabled or full. Terminal operation must continue.
  }
}

export function removeTerminalSnapshot(target: string): void {
  getStorage()?.removeItem(storageKey(target))
}
