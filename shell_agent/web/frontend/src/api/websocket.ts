import { isServerEvent, type ClientEvent, type ServerEvent } from './protocol'

export type ConnectionState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed' | 'error'
type EventListener = (event: ServerEvent) => void
type StateListener = (state: ConnectionState) => void
export const MAX_PENDING_EVENTS = 100

export class WorkbenchSocket {
  private socket: WebSocket | null = null
  private eventListeners = new Set<EventListener>()
  private stateListeners = new Set<StateListener>()
  private reconnectTimer: number | null = null
  private reconnectAttempt = 0
  private shouldReconnect = true
  private currentState: ConnectionState = 'idle'
  private pendingEvents: ClientEvent[] = []

  get state(): ConnectionState {
    return this.currentState
  }

  connect(): void {
    if (this.socket?.readyState === WebSocket.OPEN || this.socket?.readyState === WebSocket.CONNECTING) return
    this.shouldReconnect = true
    this.setState(this.reconnectAttempt > 0 ? 'reconnecting' : 'connecting')
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    this.socket = new WebSocket(`${protocol}//${window.location.host}/ws/chat`)

    this.socket.addEventListener('open', () => {
      this.reconnectAttempt = 0
      this.setState('open')
      const pending = this.pendingEvents.splice(0)
      pending.forEach((event) => this.socket?.send(JSON.stringify(event)))
    })
    this.socket.addEventListener('message', (message) => {
      try {
        const parsed: unknown = JSON.parse(String(message.data))
        if (!isServerEvent(parsed)) {
          console.warn('[ws] 忽略无效事件', parsed)
          return
        }
        this.eventListeners.forEach((listener) => listener(parsed))
      } catch (error) {
        console.warn('[ws] 无法解析服务端消息', error)
      }
    })
    this.socket.addEventListener('error', () => this.setState('error'))
    this.socket.addEventListener('close', () => {
      this.socket = null
      if (this.shouldReconnect) {
        this.scheduleReconnect()
      } else {
        this.setState('closed')
      }
    })
  }

  disconnect(): void {
    this.shouldReconnect = false
    if (this.reconnectTimer !== null) window.clearTimeout(this.reconnectTimer)
    this.reconnectTimer = null
    this.pendingEvents = []
    this.socket?.close()
    this.socket = null
    this.setState('closed')
  }

  send(event: ClientEvent): boolean {
    if (this.socket?.readyState !== WebSocket.OPEN) {
      if (this.pendingEvents.length >= MAX_PENDING_EVENTS) {
        this.pendingEvents.splice(0, this.pendingEvents.length - MAX_PENDING_EVENTS + 1)
      }
      this.pendingEvents.push(event)
      this.connect()
      return true
    }
    this.socket.send(JSON.stringify(event))
    return true
  }

  subscribe(listener: EventListener): () => void {
    this.eventListeners.add(listener)
    return () => this.eventListeners.delete(listener)
  }

  subscribeState(listener: StateListener): () => void {
    this.stateListeners.add(listener)
    listener(this.currentState)
    return () => this.stateListeners.delete(listener)
  }

  private scheduleReconnect(): void {
    this.reconnectAttempt += 1
    this.setState('reconnecting')
    const delay = Math.min(10_000, 500 * (2 ** Math.min(this.reconnectAttempt - 1, 5)))
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null
      this.connect()
    }, delay)
  }

  private setState(state: ConnectionState): void {
    if (this.currentState === state) return
    this.currentState = state
    this.stateListeners.forEach((listener) => listener(state))
  }
}

export const workbenchSocket = new WorkbenchSocket()
