import { afterEach, describe, expect, it } from 'vitest'

import type { ServerEvent } from '../api/protocol'
import {
  loadTerminalSnapshot,
  MAX_TERMINAL_HISTORY,
  MAX_TERMINAL_SCROLLBACK,
  saveTerminalSnapshot,
} from './terminalPersistence'

afterEach(() => {
  window.localStorage.clear()
})

describe('terminal persistence', () => {
  it('caps history and scrollback while retaining the newest events', () => {
    const events: ServerEvent[] = Array.from({ length: MAX_TERMINAL_SCROLLBACK + 12 }, (_, index) => ({
      type: 'system',
      timestamp: `10:00:${index}`,
      channel: 'command',
      content: index === MAX_TERMINAL_SCROLLBACK + 11 ? 'x'.repeat(25_000) : `event-${index}`,
    }))
    saveTerminalSnapshot('prod-1', {
      cwd: '/srv/app',
      history: Array.from({ length: MAX_TERMINAL_HISTORY + 9 }, (_, index) => `command-${index}`),
      events,
    })

    const restored = loadTerminalSnapshot('prod-1')
    expect(restored?.cwd).toBe('/srv/app')
    expect(restored?.history).toHaveLength(MAX_TERMINAL_HISTORY)
    expect(restored?.events).toHaveLength(MAX_TERMINAL_SCROLLBACK)
    expect(restored?.events[0]?.content).toBe('event-12')
    expect(String(restored?.events.at(-1)?.content)).toContain('本地滚屏已截断')
  })

  it('isolates snapshots by target', () => {
    saveTerminalSnapshot('dev-1', { cwd: '/dev', history: ['pwd'], events: [] })
    saveTerminalSnapshot('prod-1', { cwd: '/prod', history: ['uptime'], events: [] })
    expect(loadTerminalSnapshot('dev-1')?.cwd).toBe('/dev')
    expect(loadTerminalSnapshot('prod-1')?.cwd).toBe('/prod')
  })
})
