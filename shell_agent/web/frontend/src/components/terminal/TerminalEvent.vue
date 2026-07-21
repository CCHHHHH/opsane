<script setup lang="ts">
import type { ServerEvent } from '../../api/protocol'

const props = defineProps<{ event: ServerEvent }>()

function text(key: string, fallback = ''): string {
  const value = props.event[key]
  return value == null ? fallback : String(value)
}
</script>

<template>
  <div v-if="event.type === 'command_preview'" class="terminal-block terminal-command">
    <div class="terminal-label">{{ text('target', 'remote') }} {{ text('cwd', '~') }} $</div>
    <pre>{{ text('command') }}</pre>
    <div class="terminal-detail">风险 {{ text('risk_level', '-') }} · {{ text('confirm_mode', 'interactive') }}</div>
  </div>
  <div v-else-if="event.type === 'execution_result'" class="terminal-block" :class="event.success ? 'terminal-success' : event.partial_success ? 'terminal-warning' : 'terminal-error'">
    <pre>{{ text('output', '（无输出）') }}</pre>
    <div class="terminal-detail">exit {{ text('exit_code', '-') }} · {{ text('target', '-') }} · {{ text('cwd', '~') }}</div>
  </div>
  <div v-else-if="event.type === 'command_error'" class="terminal-line terminal-error">{{ text('content') }}</div>
  <div v-else-if="event.type === 'execution_status'" class="terminal-line text-muted">[{{ text('status') }}] {{ text('content') }}</div>
  <div v-else-if="event.type === 'system'" class="terminal-line text-muted"># {{ text('content') }}</div>
  <div v-else class="terminal-line text-muted"># {{ event.type }}</div>
</template>

<style scoped>
.terminal-block { margin-bottom: 13px; }
.terminal-block pre { margin: 0; color: var(--terminal-text); font: inherit; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; }
.terminal-command pre { color: var(--terminal-command); }
.terminal-label { margin-bottom: 3px; color: var(--success); }
.terminal-detail { margin-top: 4px; color: var(--text-muted); font-size: 11px; }
.terminal-line { margin-bottom: 7px; white-space: pre-wrap; overflow-wrap: anywhere; }
.terminal-success pre { color: var(--success-text); }
.terminal-warning pre { color: var(--warning-text); }
.terminal-error,.terminal-error pre { color: var(--danger-text); }
</style>
