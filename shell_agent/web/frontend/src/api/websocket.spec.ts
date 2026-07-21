import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ClientEvent } from './protocol'
import { MAX_PENDING_EVENTS, WorkbenchSocket } from './websocket'

class FakeWebSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  static instances: FakeWebSocket[] = []

  readyState = FakeWebSocket.CONNECTING
  readonly sent: string[] = []
  private listeners = new Map<string, Array<(event: Event) => void>>()

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this)
  }

  addEventListener(type: string, listener: (event: Event) => void): void {
    const listeners = this.listeners.get(type) ?? []
    listeners.push(listener)
    this.listeners.set(type, listeners)
  }

  send(data: string): void {
    this.sent.push(data)
  }

  open(): void {
    this.readyState = FakeWebSocket.OPEN
    this.emit('open')
  }

  close(): void {
    this.readyState = FakeWebSocket.CLOSED
    this.emit('close')
  }

  private emit(type: string): void {
    this.listeners.get(type)?.forEach((listener) => listener(new Event(type)))
  }
}

let socket: WorkbenchSocket | null = null

afterEach(() => {
  socket?.disconnect()
  socket = null
  FakeWebSocket.instances = []
  vi.unstubAllGlobals()
})

describe('WorkbenchSocket pending queue', () => {
  it('keeps the latest 100 events and flushes them in order on open', () => {
    vi.stubGlobal('WebSocket', FakeWebSocket)
    socket = new WorkbenchSocket()

    for (let index = 0; index < MAX_PENDING_EVENTS + 5; index += 1) {
      const event: ClientEvent = {
        type: 'chat',
        session_id: 'session-1',
        message: `message-${index}`,
        confirm_mode: 'interactive',
      }
      socket.send(event)
    }

    const transport = FakeWebSocket.instances[0]
    expect(transport).toBeDefined()
    transport?.open()
    expect(transport?.sent).toHaveLength(MAX_PENDING_EVENTS)
    expect(JSON.parse(transport?.sent[0] ?? '{}').message).toBe('message-5')
    expect(JSON.parse(transport?.sent.at(-1) ?? '{}').message).toBe('message-104')
  })
})
