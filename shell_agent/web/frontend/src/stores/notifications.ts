import { defineStore } from 'pinia'

export type NotificationKind = 'success' | 'error' | 'info'

export interface AppNotification {
  id: number
  kind: NotificationKind
  message: string
}

let nextNotificationId = 1

export const useNotificationsStore = defineStore('notifications', {
  state: () => ({
    items: [] as AppNotification[],
  }),
  actions: {
    show(message: string, kind: NotificationKind = 'info', duration = 3200) {
      const trimmed = message.trim()
      if (!trimmed) return
      const notification = { id: nextNotificationId, kind, message: trimmed }
      nextNotificationId += 1
      this.items.push(notification)
      globalThis.setTimeout(() => this.remove(notification.id), duration)
    },
    success(message: string) {
      this.show(message, 'success')
    },
    error(message: string) {
      this.show(message, 'error', 4800)
    },
    remove(id: number) {
      this.items = this.items.filter((item) => item.id !== id)
    },
  },
})
