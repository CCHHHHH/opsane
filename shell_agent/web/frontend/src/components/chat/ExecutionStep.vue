<script setup lang="ts">
import { computed, ref } from 'vue'

import type { CommandPreviewEvent, ExecutionResultEvent, ExecutionStatusEvent, TaskStepEvent } from '../../api/protocol'

const props = defineProps<{
  preview?: CommandPreviewEvent
  result?: ExecutionResultEvent
  status?: ExecutionStatusEvent
  taskStep?: TaskStepEvent
  actionable?: boolean
  submitting?: boolean
}>()

const emit = defineEmits<{
  confirm: [confirmed: boolean, secondaryValue: string]
}>()

const secondaryValue = ref('')

const command = computed(() => props.preview?.command || props.taskStep?.command || props.result?.command || '')
const intent = computed(() => props.preview?.intent || props.taskStep?.intent || '执行终端命令')
const target = computed(() => props.preview?.target || props.taskStep?.target || props.result?.target || '')
const output = computed(() => props.result?.output?.trim() || '')
const outputLines = computed(() => output.value ? output.value.split(/\r?\n/).length : 0)
const longOutput = computed(() => outputLines.value > 14 || output.value.length > 1800)
const riskNeedsAttention = computed(() => Boolean(
  props.preview?.risk_level && props.preview.risk_level !== 'safe'
))
const outputPreview = computed(() => {
  if (!output.value) return '（无输出）'
  const lines = output.value.split(/\r?\n/).slice(0, 8).join('\n')
  return lines.length > 1200 ? `${lines.slice(0, 1200)}…` : `${lines}\n…`
})

const state = computed(() => {
  if (props.preview?.policy_blocked) return 'failed'
  if (props.result?.timed_out) return 'timeout'
  if (props.result?.partial_success) return 'partial'
  if (props.result) return props.result.success ? 'success' : 'failed'
  if (props.submitting) return 'submitting'
  const taskStatus = String(props.taskStep?.status || '')
  if (taskStatus === 'pending') return 'waiting'
  if (['running', 'success', 'partial', 'failed', 'timeout', 'canceled'].includes(taskStatus)) return taskStatus
  const status = String(props.status?.status || '')
  if (['failed', 'timeout', 'canceled', 'stopping', 'running'].includes(status)) return status
  if (props.actionable) return 'waiting'
  if (props.preview?.confirm_mode === 'dry_run') return 'preview'
  return 'running'
})

const stateLabel = computed(() => ({
  waiting: '等待确认', submitting: '提交中', running: '执行中', stopping: '正在停止', success: '执行完成',
  partial: '部分完成', failed: props.preview?.policy_blocked ? '策略阻断' : '执行失败',
  timeout: '执行超时', canceled: '已取消', preview: '仅预览',
}[state.value] || state.value))

const resultSummary = computed(() => {
  if (!props.result) return ''
  const action = intent.value
  const exit = Number.isFinite(props.result.exit_code) ? `；退出码 ${props.result.exit_code}` : ''
  if (props.result.timed_out) return `未完成：${action}；执行已超时${exit}。`
  if (props.result.partial_success) return `已获得部分结果：${action}；返回 ${outputLines.value} 行输出${exit}。`
  if (props.result.success) {
    return outputLines.value
      ? `已完成：${action}；返回 ${outputLines.value} 行输出。`
      : `已完成：${action}；命令未返回文本输出。`
  }
  const firstLine = output.value.split(/\r?\n/).find(Boolean)
  return `未完成：${action}${exit}${firstLine ? `。返回信息：${firstLine}` : '。'}`
})

const secondaryValid = computed(() => (
  !props.preview?.requires_secondary_confirm
  || secondaryValue.value.trim() === props.preview.secondary_confirm_expected
))
</script>

<template>
  <article class="execution-step" :class="`state-${state}`">
    <header class="execution-step-head">
      <span v-if="taskStep?.step_index" class="execution-step-index">{{ taskStep.step_index }}/{{ taskStep.total_steps || '·' }}</span>
      <span class="execution-step-icon" aria-hidden="true">›_</span>
      <strong>{{ intent }}</strong>
      <span class="execution-step-status" :class="state">{{ stateLabel }}</span>
    </header>

    <div class="execution-step-meta">
      <span v-if="target">目标 {{ target }}</span>
    </div>

    <div v-if="command" class="execution-step-command">
      <span aria-hidden="true">$</span><code>{{ command }}</code>
    </div>

    <details v-if="preview?.risk_reasons?.length" class="execution-step-context" :open="riskNeedsAttention">
      <summary>查看风险说明</summary>
      <ul><li v-for="reason in preview.risk_reasons" :key="reason">{{ reason }}</li></ul>
    </details>

    <div v-if="preview?.policy_blocked" class="execution-policy-blocked">
      {{ preview.policy_block_reason || '当前环境策略已阻断该命令。' }}
    </div>

    <div v-if="actionable && preview && !preview.policy_blocked" class="execution-confirm" :aria-busy="submitting || undefined">
      <label v-if="preview.requires_secondary_confirm">
        <span>{{ preview.secondary_confirm_label || `输入 ${preview.secondary_confirm_expected} 确认` }}</span>
        <input v-model="secondaryValue" class="input" autocomplete="off" spellcheck="false" :disabled="submitting" />
        <small v-if="preview.secondary_confirm_reason">{{ preview.secondary_confirm_reason }}</small>
      </label>
      <div class="execution-confirm-actions">
        <button class="btn btn-danger btn-small" type="button" :disabled="submitting" @click="emit('confirm', false, '')">拒绝</button>
        <button class="btn btn-primary btn-small" type="button" :disabled="submitting || !secondaryValid" @click="emit('confirm', true, secondaryValue.trim())">{{ submitting ? '提交中…' : '确认执行' }}</button>
      </div>
    </div>

    <section v-if="result" class="execution-step-result" :class="{ failure: !result.success && !result.partial_success }">
      <div class="execution-result-head">
        <strong>执行输出</strong>
        <span>exit {{ result.exit_code }}</span>
      </div>
      <details v-if="longOutput && result.success" class="execution-output-details">
        <summary>查看完整输出（{{ outputLines }} 行）</summary>
        <pre>{{ output || '（无输出）' }}</pre>
      </details>
      <pre v-if="longOutput && result.success" class="execution-output-preview">{{ outputPreview }}</pre>
      <pre v-else>{{ output || '（无输出）' }}</pre>
      <p class="execution-result-summary">{{ resultSummary }}</p>
    </section>
  </article>
</template>

<style scoped>
.execution-step { --execution-color: rgba(113,147,177,.48); position: relative; width: min(860px,100%); min-width: 0; padding: 1px 0 5px 30px; box-sizing: border-box; }
.execution-step::before { content: ""; position: absolute; top: 7px; bottom: 6px; left: 9px; border-left: 1px solid var(--execution-color); }
.execution-step::after { content: ""; position: absolute; top: 7px; left: 6px; width: 7px; height: 7px; border-radius: 50%; background: var(--execution-color); box-shadow: 0 0 0 3px var(--bg-primary); }
.state-running,.state-stopping,.state-waiting,.state-submitting,.state-preview,.state-partial { --execution-color: var(--warning); }
.state-success { --execution-color: var(--success); }
.state-failed,.state-timeout,.state-canceled { --execution-color: var(--danger); }
.execution-step-head { min-height: 22px; display: flex; align-items: center; gap: 8px; }
.execution-step-index { min-width: 30px; color: var(--text-muted); font: 11px/1 ui-monospace,SFMono-Regular,Menlo,monospace; }
.execution-step-head strong { color: var(--text-primary); font-size: 14px; font-weight: 720; letter-spacing: .005em; }
.execution-step-icon { color: var(--text-muted); font: 12px/1 ui-monospace,SFMono-Regular,Menlo,monospace; }
.execution-step-status { margin-left: auto; color: var(--text-muted); font-size: 11px; }
.execution-step-status.success { color: var(--success-text); }
.execution-step-status.waiting,.execution-step-status.submitting,.execution-step-status.running,.execution-step-status.stopping,.execution-step-status.preview,.execution-step-status.partial { color: var(--warning-text); }
.execution-step-status.failed,.execution-step-status.timeout,.execution-step-status.canceled { color: var(--danger-text); }
.execution-step-meta { display: flex; flex-wrap: wrap; gap: 5px 12px; margin-top: 7px; color: var(--text-muted); font-size: 11px; }
.execution-step-command { max-width: 100%; min-width: 0; display: flex; align-items: flex-start; gap: 9px; margin-top: 9px; padding: 9px 11px; overflow-x: auto; border: 1px solid var(--border); border-radius: 6px; background: var(--terminal-bg); box-sizing: border-box; }
.execution-step-command > span { color: var(--brand-green); font: 12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; user-select: none; }
.execution-step-command code { min-width: 0; color: var(--code-accent); font: 12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
.execution-step-context { margin-top: 7px; color: var(--text-muted); font-size: 11px; }
.execution-step-context summary { width: fit-content; cursor: pointer; }
.execution-step-context ul { margin: 6px 0 0; padding-left: 18px; }
.execution-policy-blocked { margin-top: 9px; padding: 8px 10px; border-left: 2px solid var(--danger); background: var(--danger-soft); color: var(--danger-text); font-size: 12px; }
.execution-confirm { display: grid; gap: 8px; margin-top: 10px; padding: 10px; border-left: 2px solid var(--warning); border-radius: 0 6px 6px 0; background: var(--warning-soft); }
.execution-confirm label { display: grid; gap: 5px; color: var(--text-secondary); font-size: 11px; }
.execution-confirm small { color: var(--text-muted); }
.execution-confirm-actions { display: flex; justify-content: flex-start; gap: 7px; }
.execution-step-result { margin-top: 12px; padding-top: 11px; border-top: 1px solid var(--divider-soft); }
.execution-result-head { min-height: 20px; display: flex; align-items: center; gap: 10px; color: var(--text-muted); font-size: 11px; }
.execution-result-head strong { color: var(--text-secondary); font-size: 11px; }
.execution-result-head span { margin-left: auto; }
.execution-step-result pre { max-width: 100%; max-height: 360px; min-width: 0; margin: 8px 0 0; padding: 9px 11px; overflow: auto; border: 1px solid var(--border); border-radius: 6px; background: var(--terminal-bg); color: var(--text-primary); box-sizing: border-box; font: 11.5px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
.execution-step-result.failure pre { border-color: rgba(255,112,111,.38); color: var(--danger-text); }
.execution-output-details { margin-top: 8px; color: var(--text-muted); font-size: 11px; }
.execution-output-details summary { cursor: pointer; }
.execution-output-preview { color: var(--text-secondary); }
.execution-result-summary { margin: 8px 0 0; color: var(--text-secondary); font-size: 12px; line-height: 1.55; }
@media (max-width: 720px) { .execution-step { padding-left: 24px; } .execution-step::before { left: 7px; } .execution-step::after { left: 4px; } }
</style>
