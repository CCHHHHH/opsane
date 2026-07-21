<script setup lang="ts">
import { useNotificationsStore } from '../../stores/notifications'

const notifications = useNotificationsStore()
</script>

<template>
  <div class="toast-viewport" aria-live="polite" aria-atomic="false">
    <TransitionGroup name="toast-list">
      <div v-for="item in notifications.items" :key="item.id" class="app-toast" :class="`toast-${item.kind}`" role="status">
        <span class="toast-indicator" aria-hidden="true" />
        <span class="toast-message">{{ item.message }}</span>
        <button type="button" aria-label="关闭通知" title="关闭" @click="notifications.remove(item.id)">×</button>
      </div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.toast-viewport { position: fixed; z-index: 300; top: calc(var(--header-height) + 12px); right: 14px; width: min(360px,calc(100vw - 28px)); display: grid; gap: 8px; pointer-events: none; }
.app-toast { min-height: 44px; display: grid; grid-template-columns: 8px minmax(0,1fr) 26px; align-items: center; gap: 9px; padding: 8px 8px 8px 12px; border: 1px solid var(--border-light); border-radius: 8px; background: var(--glass-surface); box-shadow: var(--shadow); color: var(--text-secondary); font-size: 12px; line-height: 1.45; pointer-events: auto; backdrop-filter: blur(14px); }
.toast-indicator { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); }
.toast-success .toast-indicator { background: var(--success); }
.toast-error .toast-indicator { background: var(--danger); }
.toast-success { border-color: rgba(54,217,149,.28); }
.toast-error { border-color: rgba(255,112,111,.32); }
.toast-message { min-width: 0; overflow-wrap: anywhere; }
.app-toast button { width: 26px; height: 26px; display: grid; place-items: center; padding: 0; border: 0; border-radius: 5px; background: transparent; color: var(--text-muted); font-size: 17px; }
.app-toast button:hover { background: var(--bg-tertiary); color: var(--text-primary); }
.toast-list-enter-active,.toast-list-leave-active { transition: opacity 160ms ease, transform 160ms ease; }
.toast-list-enter-from,.toast-list-leave-to { opacity: 0; transform: translateY(-8px); }
</style>
