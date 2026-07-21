<script setup lang="ts">
import { computed, ref } from 'vue'

import type { ArtifactUploadRecord, ServerEvent } from '../../api/protocol'
import { renderAgentMarkdown } from '../../utils/markdown'

const props = defineProps<{
  event: ServerEvent
  actionable?: boolean
  submitting?: boolean
  transferStatus?: string
  planStatus?: 'waiting' | 'confirmed' | 'canceled' | 'archived'
}>()

const emit = defineEmits<{
  confirm: [confirmed: boolean, secondaryValue: string]
  planConfirm: [confirmed: boolean]
  planAdjust: [instruction: string]
  fileTransferConfirm: [confirmed: boolean]
}>()

const secondaryValue = ref('')
const adjustment = ref('')
const type = computed(() => props.event.type)
const agentHtml = computed(() => renderAgentMarkdown(text('content')))
const taskSummaryHtml = computed(() => renderAgentMarkdown(text('content', '任务已完成')))
const planState = computed(() => props.planStatus ?? (props.actionable ? 'waiting' : props.event.active ? 'confirmed' : 'archived'))
const planStateLabel = computed(() => ({
  waiting: '等待确认', confirmed: '已确认', canceled: '已取消', archived: '已归档',
}[planState.value]))
const planStateBadge = computed(() => ({
  waiting: 'badge-warning', confirmed: 'badge-success', canceled: 'badge-danger', archived: '',
}[planState.value]))
const artifact = computed<ArtifactUploadRecord>(() => (
  props.event.artifact && typeof props.event.artifact === 'object'
    ? props.event.artifact as ArtifactUploadRecord
    : {}
))
const artifactFailed = computed(() => ['failed', 'error', 'interrupted'].includes(String(artifact.value.status || '').toLowerCase()))
const transferPreview = computed<ArtifactUploadRecord>(() => (
  props.event.transfer && typeof props.event.transfer === 'object'
    ? props.event.transfer as ArtifactUploadRecord
    : {}
))
const transferPreviewStatus = computed(() => {
  if (props.submitting) return { label: '提交中', badge: 'badge-warning' }
  if (props.actionable) return { label: '等待确认', badge: 'badge-warning' }
  // The preview is immutable audit history and may remain `waiting_confirm`
  // after execution. Prefer the latest transfer/turn state supplied by the
  // page so a restored session never presents a stale confirmation status.
  const status = String(props.transferStatus || transferPreview.value.status || '').toLowerCase()
  if (['pending', 'running', 'executing'].includes(status)) return { label: '上传中', badge: 'badge-warning' }
  if (['success', 'completed', 'complete'].includes(status)) return { label: '已完成', badge: 'badge-success' }
  if (['failed', 'error', 'interrupted'].includes(status)) return { label: '失败', badge: 'badge-danger' }
  if (['cancelled', 'canceled'].includes(status)) return { label: '已取消', badge: 'badge-danger' }
  return { label: '已处理', badge: '' }
})

function text(key: string, fallback = ''): string {
  const value = props.event[key]
  return value == null ? fallback : String(value)
}

function list(key: string): string[] {
  const value = props.event[key]
  return Array.isArray(value) ? value.map(String) : []
}

function artifactText(key: keyof ArtifactUploadRecord, fallback = ''): string {
  const value = artifact.value[key] ?? props.event[key]
  return value == null ? fallback : String(value)
}

function artifactName(): string {
  return artifactText('file_name') || artifactText('filename') || artifactText('remote_name') || '会话文件'
}

function transferPreviewText(key: keyof ArtifactUploadRecord, fallback = ''): string {
  const value = transferPreview.value[key]
  return value == null ? fallback : String(value)
}

function transferPreviewName(): string {
  return transferPreviewText('file_name') || transferPreviewText('filename') || transferPreviewText('remote_name') || '会话文件'
}

function formatArtifactBytes(value: unknown): string {
  const size = Number(value)
  if (!Number.isFinite(size) || size <= 0) return ''
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`
  return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`
}

const steps = computed(() => {
  const value = props.event.steps
  return Array.isArray(value) ? value as Array<Record<string, unknown>> : []
})

function submitAdjustment() {
  if (!adjustment.value.trim()) return
  emit('planAdjust', adjustment.value.trim())
  adjustment.value = ''
}
</script>

<template>
  <article v-if="type === 'user_message'" class="timeline-item user-message">
    <div class="timeline-meta">用户 · {{ event.timestamp || '' }}</div>
    <div>{{ text('content') }}</div>
  </article>

  <article v-else-if="type === 'agent'" class="timeline-item agent-message">
    <div class="timeline-meta">Agent · {{ event.timestamp || '' }}</div>
    <div class="message-copy markdown-body" v-html="agentHtml" />
  </article>

  <div v-else-if="type === 'system'" class="system-message">{{ text('content') }}</div>

  <article v-else-if="type === 'file_transfer_preview'" class="structured-card file-transfer-preview-card">
    <div class="structured-header">
      <div><span class="eyebrow">文件传输</span><h3>{{ transferPreviewName() }}</h3></div>
      <span class="badge" :class="transferPreviewStatus.badge">{{ transferPreviewStatus.label }}</span>
    </div>
    <div class="file-transfer-destination">
      <span>将会话文件写入服务器</span>
      <code>{{ transferPreviewText('target', '服务器') }}:{{ transferPreviewText('remote_path', '-') }}</code>
    </div>
    <div class="preview-meta">
      <span v-if="transferPreview.size">大小 <strong>{{ formatArtifactBytes(transferPreview.size) }}</strong></span>
      <span v-if="transferPreviewText('sha256')">SHA-256 <code>{{ transferPreviewText('sha256').slice(0, 12) }}…</code></span>
      <span>覆盖 <strong>{{ transferPreview.overwrite ? '允许' : '禁止' }}</strong></span>
    </div>
    <div class="notice file-transfer-confirm-notice">该操作会写入远端服务器，即使处于完全访问模式也必须由你确认。</div>
    <div v-if="actionable" class="structured-actions">
      <button class="btn btn-danger" type="button" :disabled="submitting" @click="emit('fileTransferConfirm', false)">{{ submitting ? '提交中…' : '拒绝' }}</button>
      <button class="btn btn-primary" type="button" :disabled="submitting" @click="emit('fileTransferConfirm', true)">{{ submitting ? '提交中…' : '确认上传' }}</button>
    </div>
  </article>

  <article v-else-if="type === 'artifact_upload'" class="artifact-upload-event" :class="{ failed: artifactFailed }">
    <span class="artifact-upload-icon" aria-hidden="true">{{ artifactFailed ? '!' : '✓' }}</span>
    <div class="artifact-upload-copy">
      <strong>{{ artifactFailed ? '文件传输失败' : '文件已传输' }}</strong>
      <span>{{ artifactName() }}</span>
      <code v-if="artifactText('target') || artifactText('remote_path')">{{ artifactText('target', '服务器') }}:{{ artifactText('remote_path', '-') }}</code>
      <small v-if="artifactFailed">{{ artifactText('error', text('content', '传输过程中发生错误')) }}</small>
      <small v-else>
        {{ formatArtifactBytes(artifact.remote_size ?? artifact.size) }}
        <template v-if="artifactText('remote_sha256') || artifactText('sha256')"> · SHA-256 {{ (artifactText('remote_sha256') || artifactText('sha256')).slice(0, 12) }}…</template>
      </small>
    </div>
  </article>

  <article v-else-if="type === 'operation_plan'" class="structured-card plan-card">
    <div class="structured-header">
      <div><span class="eyebrow">操作方案</span><h3>{{ text('title', '待确认方案') }}</h3></div>
      <span class="badge" :class="planStateBadge">{{ planStateLabel }}</span>
    </div>
    <p v-if="text('goal')" class="plan-goal">{{ text('goal') }}</p>
    <div v-if="text('recommended_approach')" class="plan-approach"><strong>推荐方式</strong><span>{{ text('recommended_approach') }}</span></div>
    <ol v-if="steps.length" class="plan-steps">
      <li v-for="(step, index) in steps" :key="index">
        <strong>{{ String(step.intent ?? `步骤 ${index + 1}`) }}</strong>
        <code v-if="step.command">{{ String(step.command) }}</code>
        <span v-if="step.explanation">{{ String(step.explanation) }}</span>
      </li>
    </ol>
    <div v-if="list('risks').length || list('verification').length" class="plan-columns">
      <div v-if="list('risks').length"><strong>风险</strong><ul><li v-for="item in list('risks')" :key="item">{{ item }}</li></ul></div>
      <div v-if="list('verification').length"><strong>验证</strong><ul><li v-for="item in list('verification')" :key="item">{{ item }}</li></ul></div>
    </div>
    <template v-if="actionable">
      <form class="plan-adjust" @submit.prevent="submitAdjustment">
        <input v-model="adjustment" class="input" placeholder="输入方案调整要求" />
        <button class="btn btn-small" type="submit">调整</button>
      </form>
      <div class="structured-actions">
        <button class="btn btn-danger" type="button" @click="emit('planConfirm', false)">取消方案</button>
        <button class="btn btn-primary" type="button" @click="emit('planConfirm', true)">确认方案</button>
      </div>
    </template>
  </article>

  <article v-else-if="type === 'command_preview'" class="structured-card command-preview-card">
    <div class="structured-header">
      <div><span class="eyebrow">命令预览</span><h3>{{ text('intent', '准备执行命令') }}</h3></div>
      <span class="badge" :class="`risk-${text('risk_level', 'caution')}`">{{ text('risk_level', 'unknown') }}</span>
    </div>
    <pre class="code-block">{{ text('command') }}</pre>
    <div class="preview-meta"><span>目标 <strong>{{ text('target', '-') }}</strong></span><span v-if="text('cwd')">目录 <code>{{ text('cwd') }}</code></span><span>模式 {{ text('confirm_mode', 'interactive') }}</span></div>
    <ul v-if="list('risk_reasons').length" class="risk-list"><li v-for="reason in list('risk_reasons')" :key="reason">{{ reason }}</li></ul>
    <div v-if="event.policy_blocked" class="notice notice-error">{{ text('policy_block_reason', '环境策略已阻断') }}</div>
    <label v-if="actionable && event.requires_secondary_confirm" class="field secondary-field">
      <span class="field-label">{{ text('secondary_confirm_label', `输入 ${text('secondary_confirm_expected')} 确认`) }}</span>
      <input v-model="secondaryValue" class="input" autocomplete="off" spellcheck="false" />
      <small class="text-muted">{{ text('secondary_confirm_reason') }}</small>
    </label>
    <div v-if="actionable && !event.policy_blocked" class="structured-actions">
      <button class="btn btn-danger" type="button" @click="emit('confirm', false, '')">拒绝</button>
      <button
        class="btn btn-primary"
        type="button"
        :disabled="Boolean(event.requires_secondary_confirm) && secondaryValue.trim() !== text('secondary_confirm_expected')"
        @click="emit('confirm', true, secondaryValue.trim())"
      >确认执行</button>
    </div>
  </article>

  <article v-else-if="type === 'execution_result'" class="structured-card result-card">
    <div class="structured-header">
      <div><span class="eyebrow">执行结果</span><h3>{{ event.success ? '执行成功' : event.partial_success ? '返回部分结果' : '执行失败' }}</h3></div>
      <span class="badge" :class="event.success ? 'badge-success' : event.partial_success ? 'badge-warning' : 'badge-danger'">exit {{ text('exit_code', '-') }}</span>
    </div>
    <section v-if="text('command')" class="result-section">
      <span class="result-label">执行命令</span>
      <pre class="code-block result-command">{{ text('command') }}</pre>
    </section>
    <div v-if="text('target') || text('cwd')" class="result-meta">
      <span v-if="text('target')">目标 <strong>{{ text('target') }}</strong></span>
      <span v-if="text('cwd')">目录 <code>{{ text('cwd') }}</code></span>
    </div>
    <section class="result-section result-output-section">
      <span class="result-label">执行输出</span>
      <pre class="code-block result-output">{{ text('output', '（无输出）') }}</pre>
    </section>
  </article>

  <article v-else-if="type === 'task_step' && text('status') === 'complete'" class="structured-card task-summary-card">
    <div class="structured-header">
      <div><span class="eyebrow">任务总结</span><h3>{{ text('intent', '任务完成') }}</h3></div>
      <span class="badge badge-success">complete</span>
    </div>
    <div class="task-summary-body markdown-body" v-html="taskSummaryHtml" />
  </article>

  <div v-else-if="type === 'task_step'" class="task-step" :class="`task-${text('status')}`">
    <span class="task-index">{{ text('step_index', '·') }}/{{ text('total_steps', '·') }}</span>
    <span><strong>{{ text('intent', '任务步骤') }}</strong><small>{{ text('content') }}</small></span>
    <span class="badge">{{ text('status') }}</span>
  </div>

  <div v-else-if="type === 'turn_state'" class="turn-state" :class="{ active: event.active }">
    <span class="status-dot" :class="{ online: event.active }" />
    <span>{{ text('label', text('status')) }}</span>
  </div>

  <div v-else-if="type === 'execution_status'" class="turn-state" :class="{ active: ['running', 'stopping'].includes(text('status')) }">
    <span class="status-dot" :class="{ online: ['running', 'stopping'].includes(text('status')) }" />
    <span>{{ text('content', text('status')) }}</span>
  </div>

  <article v-else class="system-message">收到事件：{{ type }}</article>
</template>

<style scoped>
.timeline-item { max-width: min(780px,86%); min-width: 0; line-height: 1.62; white-space: pre-wrap; overflow-wrap: anywhere; }
.user-message { align-self: flex-end; margin-left: 56px; padding: 10px 13px; border: 1px solid rgba(24,184,231,.2); border-radius: 15px 15px 4px 15px; background: var(--bg-elevated); box-shadow: 0 8px 24px rgba(0,34,58,.08); }
.agent-message { width: min(920px,100%); max-width: 100%; align-self: flex-start; padding: 0; border: 0; background: transparent; }
.timeline-meta { display: none; }
.message-copy { white-space: normal; }
.markdown-body { overflow-wrap: anywhere; }
.markdown-body :deep(> :first-child) { margin-top: 0; }
.markdown-body :deep(> :last-child) { margin-bottom: 0; }
.markdown-body :deep(p) { margin: 0 0 10px; }
.markdown-body :deep(h1),.markdown-body :deep(h2),.markdown-body :deep(h3),.markdown-body :deep(h4) { margin: 16px 0 8px; line-height: 1.3; }
.markdown-body :deep(h1) { font-size: 19px; }
.markdown-body :deep(h2) { font-size: 17px; }
.markdown-body :deep(h3),.markdown-body :deep(h4) { font-size: 14px; }
.markdown-body :deep(ul),.markdown-body :deep(ol) { margin: 8px 0 10px; padding-left: 24px; }
.markdown-body :deep(li + li) { margin-top: 4px; }
.markdown-body :deep(blockquote) { margin: 10px 0; padding: 2px 12px; border-left: 3px solid var(--border-light); color: var(--text-secondary); }
.markdown-body :deep(code) { padding: 2px 5px; border-radius: 4px; background: var(--bg-primary); color: var(--code-accent); font-size: .92em; }
.markdown-body :deep(pre) { max-width: 100%; overflow: auto; margin: 10px 0; padding: 10px 12px; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-primary); }
.markdown-body :deep(pre code) { padding: 0; background: transparent; color: var(--text-primary); font-size: 11.5px; white-space: pre; }
.markdown-body :deep(table) { width: 100%; display: block; overflow-x: auto; margin: 10px 0; border-spacing: 0; border-collapse: collapse; font-size: 12px; }
.markdown-body :deep(th),.markdown-body :deep(td) { min-width: 90px; padding: 6px 9px; border: 1px solid var(--border); text-align: left; }
.markdown-body :deep(th) { background: var(--bg-tertiary); }
.markdown-body :deep(a) { color: var(--accent); text-decoration: underline; text-underline-offset: 2px; }
.markdown-body :deep(hr) { margin: 14px 0; border: 0; border-top: 1px solid var(--border); }
.system-message { align-self: center; max-width: 760px; padding: 4px 10px; color: var(--text-muted); font-size: 12px; text-align: center; }
.artifact-upload-event { width: min(100%,720px); display: flex; align-items: flex-start; gap: 10px; padding: 10px 12px; border-left: 2px solid var(--success); border-radius: 0 8px 8px 0; background: var(--success-soft); }
.artifact-upload-event.failed { border-left-color: var(--danger); background: var(--danger-soft); }
.artifact-upload-icon { width: 22px; height: 22px; flex: 0 0 22px; display: grid; place-items: center; border-radius: 50%; background: var(--success-soft); color: var(--success); font-size: 12px; font-weight: 750; }
.artifact-upload-event.failed .artifact-upload-icon { background: rgba(255,112,111,.18); color: var(--danger); }
.artifact-upload-copy { min-width: 0; display: grid; gap: 3px; }
.artifact-upload-copy strong { font-size: 12px; }
.artifact-upload-copy > span { color: var(--text-secondary); font-size: 12px; }
.artifact-upload-copy code { overflow-wrap: anywhere; color: var(--code-accent); font-size: 10.5px; }
.artifact-upload-copy small { color: var(--text-muted); font-size: 10px; }
.file-transfer-destination { display: grid; gap: 7px; padding: 13px 14px 10px; color: var(--text-secondary); font-size: 12px; }
.file-transfer-destination code { padding: 9px 10px; overflow-wrap: anywhere; border: 1px solid var(--border); border-radius: 7px; background: var(--bg-primary); color: var(--code-accent); }
.file-transfer-confirm-notice { margin: 0 14px 13px; padding: 9px 10px; border-left: 2px solid var(--warning); border-radius: 0 6px 6px 0; background: var(--warning-soft); color: var(--text-secondary); font-size: 11px; }
.structured-card { width: min(100%, 840px); align-self: flex-start; overflow: hidden; border: 1px solid var(--border); border-radius: 10px; background: var(--bg-secondary); }
.structured-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; padding: 13px 14px; border-bottom: 1px solid var(--border); }
.structured-header h3 { margin: 3px 0 0; font-size: 14px; }
.eyebrow { color: var(--text-muted); font-size: 10px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; }
.structured-card .code-block { margin: 13px 14px; }
.structured-actions { display: flex; justify-content: flex-start; gap: 8px; padding: 11px 14px; border-top: 1px solid var(--border); }
.preview-meta { display: flex; gap: 14px; padding: 0 14px 12px; color: var(--text-muted); font-size: 11px; }
.risk-list { margin: 0 14px 12px; padding-left: 18px; color: var(--warning); font-size: 12px; }
.risk-safe { border-color: rgba(54,217,149,.35); color: var(--success); }
.risk-caution { border-color: rgba(241,187,97,.35); color: var(--warning); }
.risk-dangerous,.risk-critical { border-color: rgba(255,112,111,.35); color: var(--danger); }
.secondary-field { margin: 0 14px 13px; padding: 10px; border: 1px solid rgba(241,187,97,.3); border-radius: 8px; background: var(--warning-soft); }
.plan-goal { margin: 14px; color: var(--text-secondary); line-height: 1.55; }
.plan-approach { display: grid; gap: 5px; margin: 0 14px 13px; padding: 10px; border-left: 3px solid var(--accent); background: var(--accent-soft); }
.plan-steps { display: grid; gap: 9px; margin: 0 14px 14px; padding-left: 26px; }
.plan-steps li { padding-left: 3px; color: var(--text-secondary); }
.plan-steps strong,.plan-steps code,.plan-steps span { display: block; margin-bottom: 4px; }
.plan-steps code { padding: 6px 8px; border-radius: 5px; background: var(--bg-primary); color: var(--text-primary); }
.plan-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 0 14px 14px; color: var(--text-secondary); font-size: 12px; }
.plan-columns > div { padding: 10px; border: 1px solid var(--border); border-radius: 7px; }
.plan-columns ul { margin: 6px 0 0; padding-left: 18px; }
.plan-adjust { display: flex; gap: 8px; padding: 0 14px 12px; }
.result-section { padding: 12px 14px 0; }
.result-section .code-block { margin: 6px 0 0; }
.result-output-section { padding-bottom: 13px; }
.result-label { display: block; color: var(--text-muted); font-size: 10px; font-weight: 600; letter-spacing: .06em; }
.result-meta { display: flex; flex-wrap: wrap; gap: 14px; padding: 10px 14px 0; color: var(--text-muted); font-size: 11px; }
.result-command { max-height: 240px; }
.result-output { max-height: 380px; }
.task-summary-card { border: 0; border-radius: 0; background: transparent; }
.task-summary-card .structured-header { padding: 0 0 9px; border-bottom-color: var(--divider-soft); }
.task-summary-body { padding: 12px 0 0; color: var(--text-secondary); line-height: 1.6; }
.task-step { width: min(100%, 840px); display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 10px; padding: 9px 11px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg-secondary); }
.task-step > span:nth-child(2) { display: grid; gap: 2px; }
.task-step small { color: var(--text-muted); }
.task-index { color: var(--text-muted); font-family: monospace; }
.turn-state { align-self: center; display: inline-flex; align-items: center; gap: 7px; padding: 4px 9px; border: 1px solid var(--border); border-radius: 999px; color: var(--text-muted); font-size: 11px; }
@media (max-width: 700px) { .timeline-item { max-width: 96%; } .plan-columns { grid-template-columns: 1fr; } .preview-meta { flex-direction: column; gap: 4px; } }
</style>
