<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import { errorMessage } from '../api/http'
import SessionListItem from '../components/common/SessionListItem.vue'
import TerminalEvent from '../components/terminal/TerminalEvent.vue'
import { useInventoryStore } from '../stores/inventory'
import { useNotificationsStore } from '../stores/notifications'
import { useSessionsStore } from '../stores/sessions'
import { useTerminalStore } from '../stores/terminal'
import { confirmAction } from '../utils/confirm'

const terminal = useTerminalStore()
const inventory = useInventoryStore()
const sessions = useSessionsStore()
const notifications = useNotificationsStore()
const command = ref('')
const search = ref('')
const secondaryValue = ref('')
const output = ref<HTMLElement | null>(null)
const commandInput = ref<HTMLInputElement | null>(null)
const candidates = ref<string[]>([])
const completionRequestId = ref('')
const historyIndex = ref(-1)

const latestStatus = computed(() => {
  for (let index = terminal.entries.length - 1; index >= 0; index -= 1) {
    const event = terminal.entries[index]?.event
    if (event?.type === 'execution_status') return String(event.status ?? '')
  }
  return ''
})
const running = computed(() => ['running', 'stopping'].includes(latestStatus.value))

async function selectSession(id: string) {
  const detail = await sessions.select(id, 300)
  terminal.setSession(id)
  terminal.hydrate(detail.messages)
  terminal.restorePending(detail.pending)
}

async function loadSessions() {
  await sessions.load('command', search.value)
  if (terminal.sessionId && sessions.items.some((item) => item.id === terminal.sessionId)) return
  if (sessions.items[0]) await selectSession(sessions.items[0].id)
  else if (!search.value) await createSession()
}

async function createSession() {
  const session = await sessions.create('command')
  terminal.setSession(session.id)
  terminal.hydrate(sessions.selected?.messages)
  terminal.restorePending(sessions.selected?.pending)
}

async function removeSession(id: string) {
  if (!confirmAction('确定删除这个终端会话吗？')) return
  try {
    await sessions.remove(id)
    notifications.success('终端会话已删除')
    if (terminal.sessionId === id) {
      terminal.setSession('')
      await loadSessions()
    }
  } catch (error) {
    notifications.error(errorMessage(error))
  }
}

async function renameSession(id: string, title: string) {
  try {
    await sessions.rename(id, title)
    notifications.success('会话已重命名')
  } catch (error) {
    notifications.error(errorMessage(error))
  }
}

async function pinSession(id: string, pinned: boolean) {
  try {
    await sessions.pin(id, pinned)
    await sessions.load('command', search.value)
    notifications.success(pinned ? '会话已置顶' : '已取消置顶')
  } catch (error) {
    notifications.error(errorMessage(error))
  }
}

function runCommand() {
  const value = command.value.trim()
  if (!value) return
  terminal.run(value)
  historyIndex.value = -1
  command.value = ''
  candidates.value = []
}

function requestCompletion(event: KeyboardEvent) {
  event.preventDefault()
  const cursor = commandInput.value?.selectionStart ?? command.value.length
  completionRequestId.value = `completion-${Date.now()}`
  terminal.complete(command.value, cursor, completionRequestId.value)
}

function applyCandidate(candidate: string) {
  const completion = terminal.completion
  if (!completion) return
  command.value = `${command.value.slice(0, completion.start)}${candidate}${command.value.slice(completion.end)}`
  candidates.value = []
  void nextTick(() => {
    const cursor = completion.start + candidate.length
    commandInput.value?.focus()
    commandInput.value?.setSelectionRange(cursor, cursor)
  })
}

function browseHistory(direction: number) {
  if (!terminal.history.length) return
  historyIndex.value = Math.max(-1, Math.min(terminal.history.length - 1, historyIndex.value + direction))
  command.value = historyIndex.value < 0 ? '' : terminal.history[historyIndex.value] ?? ''
  void nextTick(() => commandInput.value?.setSelectionRange(command.value.length, command.value.length))
}

function changeTarget(event: Event) {
  terminal.selectTarget((event.target as HTMLSelectElement).value)
  historyIndex.value = -1
  command.value = ''
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key === 'Tab') return requestCompletion(event)
  if (event.key === 'ArrowUp') {
    event.preventDefault()
    browseHistory(1)
  } else if (event.key === 'ArrowDown') {
    event.preventDefault()
    browseHistory(-1)
  }
}

watch(() => terminal.entries.length, async () => {
  await nextTick()
  if (output.value) output.value.scrollTop = output.value.scrollHeight
})

watch(() => terminal.completion, (result) => {
  if (!result || result.request_id !== completionRequestId.value) return
  if (result.common_prefix && result.common_prefix !== result.prefix) {
    applyCandidate(result.common_prefix)
  } else {
    candidates.value = result.candidates
  }
})

watch(command, () => { candidates.value = [] })

onMounted(async () => {
  terminal.connect()
  await inventory.load()
  if (!terminal.target && inventory.servers[0]) terminal.selectTarget(inventory.servers[0].alias)
  await loadSessions()
  commandInput.value?.focus()
})
</script>

<template>
  <section class="terminal-layout">
    <aside class="terminal-sidebar">
      <div class="sidebar-header">
        <div class="sidebar-title"><span>终端会话</span><button class="btn btn-small btn-primary" type="button" @click="createSession">＋</button></div>
        <div class="session-search"><input v-model="search" class="input" placeholder="搜索会话" @keyup.enter="loadSessions" /><button class="btn btn-small" type="button" @click="loadSessions">搜索</button></div>
      </div>
      <div class="session-list">
        <SessionListItem
          v-for="session in sessions.items"
          :key="session.id"
          :session="session"
          :active="session.id === terminal.sessionId"
          @select="selectSession(session.id)"
          @rename="(title) => renameSession(session.id, title)"
          @pin="(pinned) => pinSession(session.id, pinned)"
          @remove="removeSession(session.id)"
        />
      </div>
    </aside>

    <div class="terminal-workspace">
      <div class="terminal-toolbar">
        <div class="terminal-title"><strong>{{ sessions.selected?.title || '远程命令终端' }}</strong><span class="badge" :class="terminal.connectionState === 'open' ? 'badge-success' : 'badge-warning'">{{ terminal.connectionState }}</span></div>
        <div class="toolbar-controls">
          <select :value="terminal.target" class="select compact-select" @change="changeTarget">
            <option value="">请选择目标</option>
            <option v-for="server in inventory.servers" :key="server.alias" :value="server.alias">{{ server.alias }} · {{ server.env }}</option>
          </select>
        </div>
      </div>

      <div ref="output" class="terminal-output" role="log" aria-live="polite">
        <div class="terminal-banner">
          <span>Opsane Remote Console</span>
          <small>会话 {{ terminal.sessionId || '-' }} · WebSocket {{ terminal.connectionState }}</small>
        </div>
        <div v-if="!terminal.entries.length" class="terminal-placeholder"># 选择目标服务器后输入命令。所有命令会先经过风险分类与环境策略。</div>
        <TerminalEvent v-for="entry in terminal.entries" :key="entry.id" :event="entry.event" />
      </div>

      <div v-if="terminal.pendingPreview" class="terminal-confirm">
        <div>
          <strong>等待人工确认</strong>
          <span>{{ terminal.pendingPreview.target }} · {{ terminal.pendingPreview.risk_level }}</span>
        </div>
        <input
          v-if="terminal.pendingPreview.requires_secondary_confirm"
          v-model="secondaryValue"
          class="input secondary-input"
          :placeholder="terminal.pendingPreview.secondary_confirm_label || `输入 ${terminal.pendingPreview.secondary_confirm_expected}`"
        />
        <button class="btn btn-danger btn-small" type="button" @click="terminal.confirm(false)">拒绝</button>
        <button
          class="btn btn-primary btn-small"
          type="button"
          :disabled="terminal.pendingPreview.requires_secondary_confirm && secondaryValue.trim() !== terminal.pendingPreview.secondary_confirm_expected"
          @click="terminal.confirm(true, secondaryValue.trim())"
        >确认执行</button>
      </div>

      <form class="terminal-input-wrap" @submit.prevent="runCommand">
        <div class="terminal-prompt"><span>{{ terminal.target || 'target' }}</span><span>:</span><span>{{ terminal.cwd || '~' }}</span><span>$</span></div>
        <input
          ref="commandInput"
          v-model="command"
          class="terminal-input"
          autocomplete="off"
          spellcheck="false"
          :disabled="!terminal.sessionId"
          @keydown="handleKeydown"
        />
        <button v-if="running" class="btn btn-danger btn-small" type="button" @click="terminal.cancel">停止</button>
        <button v-else class="btn btn-primary btn-small" type="submit" :disabled="!command.trim() || !terminal.sessionId">执行</button>
        <div v-if="candidates.length" class="completion-menu">
          <button v-for="candidate in candidates.slice(0, 16)" :key="candidate" type="button" @click="applyCandidate(candidate)">{{ candidate }}</button>
        </div>
      </form>
    </div>
  </section>
</template>

<style scoped>
.terminal-layout { min-height: 0; height: 100%; display: grid; grid-template-columns: 230px minmax(0,1fr); overflow: hidden; background: var(--terminal-bg); }
.terminal-sidebar { min-width: 0; display: flex; flex-direction: column; border-right: 1px solid var(--border); background: var(--bg-secondary); }
.sidebar-header { display: grid; gap: 9px; padding: 10px; border-bottom: 1px solid var(--border); }
.sidebar-title { display: flex; align-items: center; justify-content: space-between; color: var(--text-secondary); font-size: 12px; font-weight: 600; }
.session-search { display: flex; align-items: center; gap: 5px; }
.session-search .input { min-width: 0; min-height: 28px; height: 28px; flex: 1; padding: 4px 8px; font-size: 11px; }
.session-search .btn { min-height: 28px; height: 28px; flex: 0 0 auto; padding: 4px 8px; line-height: 1; white-space: nowrap; }
.session-list { flex: 1; overflow: auto; padding: 7px; }
.terminal-workspace { min-width: 0; min-height: 0; height: 100%; display: grid; grid-template-rows: 46px minmax(0,1fr) auto auto; overflow: hidden; }
.terminal-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 0 14px; border-bottom: 1px solid var(--border); background: var(--bg-secondary); }
.terminal-title { display: flex; align-items: center; gap: 9px; }
.toolbar-controls { display: flex; gap: 7px; }
.compact-select { width: auto; min-width: 150px; min-height: 31px; padding: 4px 26px 4px 8px; font-size: 11px; }
.terminal-output { min-height: 0; overflow: auto; padding: 18px; color: var(--terminal-text); font: 12.5px/1.6 "SFMono-Regular", Consolas, monospace; }
.terminal-banner { display: grid; gap: 2px; margin-bottom: 16px; color: var(--accent); }
.terminal-banner small { color: var(--text-muted); }
.terminal-placeholder { color: var(--text-muted); }
.terminal-confirm { display: flex; align-items: center; gap: 8px; padding: 9px 13px; border-top: 1px solid rgba(241,187,97,.35); background: var(--warning-soft); }
.terminal-confirm > div { display: grid; gap: 2px; margin-right: auto; }
.terminal-confirm span { color: var(--text-muted); font-size: 10px; }
.secondary-input { width: 220px; min-height: 30px; }
.terminal-input-wrap { position: relative; display: flex; align-items: center; gap: 8px; padding: 10px 13px; border-top: 1px solid var(--border); background: var(--bg-primary); }
.terminal-prompt { display: flex; color: var(--success); font: 12px "SFMono-Regular", Consolas, monospace; white-space: nowrap; }
.terminal-input { min-width: 0; flex: 1; border: 0; outline: 0; background: transparent; color: var(--text-primary); font: 13px "SFMono-Regular", Consolas, monospace; }
.completion-menu { position: absolute; left: 13px; bottom: calc(100% + 4px); max-width: 520px; max-height: 240px; display: flex; flex-wrap: wrap; gap: 3px; overflow: auto; padding: 7px; border: 1px solid var(--border-light); border-radius: 7px; background: var(--bg-secondary); box-shadow: var(--shadow); }
.completion-menu button { padding: 5px 7px; border: 0; border-radius: 4px; background: transparent; color: var(--text-secondary); font: 11px monospace; }
.completion-menu button:hover { background: var(--accent); color: #fff; box-shadow: 0 4px 12px rgba(0,188,232,.15); }
@media (max-width: 760px) { .terminal-layout { grid-template-columns: 1fr; } .terminal-sidebar { display: none; } }
</style>
