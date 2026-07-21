<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import type {
  DeploymentRunRecord,
  DeploymentRunStatus,
  DeploymentRunStep,
} from '../../api/deployments'

const props = defineProps<{
  run: DeploymentRunRecord
  pendingAction?: string
  error?: string
}>()

const emit = defineEmits<{
  confirm: []
  cancel: []
  rollbackConfirm: []
}>()

const localAction = ref('')

const statusLabels: Record<DeploymentRunStatus, string> = {
  created: '准备中',
  precheck_running: '部署前检查',
  waiting_plan_confirm: '等待确认方案',
  confirmed: '方案已确认',
  lock_acquiring: '正在获取部署锁',
  locked: '已锁定服务',
  staging_upload: '正在上传制品',
  artifact_verified: '制品校验通过',
  backup_running: '正在备份',
  stopping: '正在停止服务',
  switching: '正在替换制品',
  starting: '正在启动服务',
  postcheck_running: '正在验证服务',
  succeeded: '部署已通过验证',
  finalizing: '正在收尾',
  completed: '部署完成',
  precheck_failed: '部署前检查失败',
  plan_rejected: '方案已取消',
  lock_conflict: '服务正在部署',
  step_failed: '部署失败',
  rollback_required: '需要回滚',
  rollback_confirmed: '回滚已确认',
  rollback_running: '正在回滚',
  rollback_postcheck: '正在验证回滚',
  rolled_back: '已安全回滚',
  rollback_failed: '回滚失败',
  manual_intervention: '需要人工处理',
  canceled: '已取消',
  unknown: '远端状态未知',
}

const runningStatuses = new Set<DeploymentRunStatus>([
  'created', 'precheck_running', 'confirmed', 'lock_acquiring', 'locked', 'staging_upload',
  'artifact_verified', 'backup_running', 'stopping', 'switching', 'starting',
  'postcheck_running', 'succeeded', 'finalizing', 'rollback_confirmed', 'rollback_running',
  'rollback_postcheck',
])
const dangerStatuses = new Set<DeploymentRunStatus>([
  'precheck_failed', 'lock_conflict', 'step_failed', 'rollback_required', 'rollback_failed',
  'manual_intervention', 'unknown',
])
const successStatuses = new Set<DeploymentRunStatus>(['completed', 'rolled_back'])
const cancelableStatuses = new Set<DeploymentRunStatus>([
  'created', 'waiting_plan_confirm', 'confirmed',
])
const rollbackStatuses = new Set<DeploymentRunStatus>([
  'rollback_required', 'rollback_confirmed', 'rollback_running', 'rollback_postcheck',
  'rolled_back', 'rollback_failed', 'manual_intervention',
])

const service = computed(() => props.run.plan.service ?? {})
const artifact = computed(() => props.run.plan.artifact ?? {})
const title = computed(() => service.value.service_name || props.run.service_id || '服务部署')
const statusLabel = computed(() => statusLabels[props.run.status] ?? props.run.status)
const statusTone = computed(() => {
  if (successStatuses.has(props.run.status)) return 'success'
  if (dangerStatuses.has(props.run.status)) return 'danger'
  if (props.run.status === 'waiting_plan_confirm' || props.run.status === 'plan_rejected') return 'warning'
  return runningStatuses.has(props.run.status) ? 'running' : 'muted'
})
const busy = computed(() => Boolean(props.pendingAction || localAction.value))
const primarySteps = computed(() => props.run.steps.filter((step) => (
  !['rollback', 'rollback_postcheck'].includes(step.phase)
)))
const rollbackSteps = computed(() => props.run.steps.filter((step) => (
  ['rollback', 'rollback_postcheck'].includes(step.phase)
)))
const showRollbackSteps = computed(() => rollbackStatuses.has(props.run.status))
const currentStep = computed(() => props.run.steps.find((step) => step.status === 'running'))
const completedCount = computed(() => primarySteps.value.filter((step) => (
  ['success', 'skipped'].includes(step.status)
)).length)
const progress = computed(() => primarySteps.value.length
  ? Math.round(completedCount.value / primarySteps.value.length * 100)
  : 0)
const criticalMessage = computed(() => {
  if (props.run.status === 'unknown') {
    return '无法确认远端操作是否完成。系统不会盲目重试，请先核对服务、制品和进程状态。'
  }
  if (props.run.status === 'manual_intervention' || props.run.status === 'rollback_failed') {
    return '自动恢复未能确认服务安全，部署锁会保留。请由运维人员核对远端状态后处理。'
  }
  if (props.run.status === 'rollback_required') {
    return '部署已经改变远端服务且验证未通过。请确认恢复部署前制品并重新验证服务。'
  }
  return ''
})

watch(
  () => [props.pendingAction, props.run.status],
  ([pending]) => {
    if (!pending) localAction.value = ''
  },
)

function phaseLabel(phase: string): string {
  return ({
    precheck: '检查', execute: '部署', postcheck: '验证', rollback: '回滚',
    rollback_postcheck: '回滚验证',
  } as Record<string, string>)[phase] ?? phase
}

function stepStatusLabel(step: DeploymentRunStep): string {
  return ({
    pending: '待执行', running: '执行中', success: '已完成', failed: '失败',
    skipped: '已跳过', unknown: '状态未知',
  } as Record<string, string>)[step.status] ?? step.status
}

function action(action: 'confirm' | 'cancel' | 'rollback') {
  if (busy.value) return
  localAction.value = action
  if (action === 'confirm') emit('confirm')
  else if (action === 'rollback') emit('rollbackConfirm')
  else emit('cancel')
}

function shortHash(value = ''): string {
  if (!value) return '-'
  return value.length > 16 ? `${value.slice(0, 12)}…` : value
}

function formatBytes(value: unknown): string {
  const size = Number(value)
  if (!Number.isFinite(size) || size <= 0) return '-'
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}
</script>

<template>
  <article class="deployment-run-card" :class="`tone-${statusTone}`" :data-run-status="run.status">
    <header class="deployment-run-header">
      <div class="deployment-title">
        <span class="deployment-eyebrow">部署 Runbook</span>
        <h3>{{ title }}</h3>
      </div>
      <span class="deployment-status" :class="statusTone">
        <span v-if="runningStatuses.has(run.status)" class="deployment-status-dot" aria-hidden="true" />
        {{ statusLabel }}
      </span>
    </header>

    <div class="deployment-meta">
      <span><small>目标</small>{{ run.target || service.target || '-' }}</span>
      <span><small>环境</small>{{ run.environment || service.environment || '-' }}</span>
      <span><small>制品</small>{{ artifact.name || '-' }}</span>
      <span><small>大小</small>{{ formatBytes(artifact.size) }}</span>
    </div>

    <div class="deployment-progress" :aria-label="`部署进度 ${progress}%`">
      <span :style="{ width: `${progress}%` }" />
    </div>

    <section v-if="run.status === 'waiting_plan_confirm'" class="deployment-plan-notice">
      <strong>部署前检查已通过</strong>
      <p>请确认下面的冻结方案。确认仅对当前目标、制品校验值和步骤生效，方案发生变化后必须重新确认。</p>
      <span>方案校验 {{ shortHash(run.plan_hash) }}</span>
    </section>

    <section v-if="criticalMessage" class="deployment-critical" role="alert">
      <strong>{{ statusLabel }}</strong>
      <p>{{ criticalMessage }}</p>
    </section>

    <ol class="deployment-steps" aria-label="部署步骤">
      <li
        v-for="(step, index) in primarySteps"
        :key="step.step_id"
        class="deployment-step"
        :class="[`step-${step.status}`, { current: currentStep?.step_id === step.step_id }]"
      >
        <span class="deployment-step-marker" aria-hidden="true">{{ step.status === 'success' ? '✓' : index + 1 }}</span>
        <div class="deployment-step-copy">
          <div><strong>{{ step.name }}</strong><span>{{ phaseLabel(step.phase) }}</span></div>
          <small>{{ stepStatusLabel(step) }}</small>
          <pre v-if="step.error || step.stderr">{{ step.error || step.stderr }}</pre>
          <details v-else-if="step.stdout">
            <summary>查看步骤输出</summary>
            <pre>{{ step.stdout }}</pre>
          </details>
        </div>
      </li>
    </ol>

    <details v-if="rollbackSteps.length && !showRollbackSteps" class="deployment-rollback-plan">
      <summary>查看失败回滚方案</summary>
      <ol><li v-for="step in rollbackSteps" :key="step.step_id">{{ step.name }}</li></ol>
    </details>

    <section v-if="showRollbackSteps && rollbackSteps.length" class="deployment-rollback-flow">
      <strong>回滚步骤</strong>
      <ol class="deployment-steps">
        <li v-for="(step, index) in rollbackSteps" :key="step.step_id" class="deployment-step" :class="`step-${step.status}`">
          <span class="deployment-step-marker" aria-hidden="true">{{ step.status === 'success' ? '✓' : index + 1 }}</span>
          <div class="deployment-step-copy"><div><strong>{{ step.name }}</strong><span>{{ phaseLabel(step.phase) }}</span></div><small>{{ stepStatusLabel(step) }}</small></div>
        </li>
      </ol>
    </section>

    <div v-if="run.error || error" class="deployment-error" role="alert">{{ run.error || error }}</div>
    <p v-if="run.result_summary" class="deployment-result">{{ run.result_summary }}</p>

    <footer v-if="run.status === 'waiting_plan_confirm'" class="deployment-actions">
      <button class="btn btn-danger" data-action="cancel" type="button" :disabled="busy" @click="action('cancel')">
        {{ busy && localAction === 'cancel' ? '取消中…' : '取消部署' }}
      </button>
      <button class="btn btn-primary" data-action="confirm" type="button" :disabled="busy" @click="action('confirm')">
        {{ busy && localAction === 'confirm' ? '确认中…' : '确认并开始部署' }}
      </button>
    </footer>
    <footer v-else-if="run.status === 'rollback_required'" class="deployment-actions">
      <button class="btn btn-danger" data-action="rollback" type="button" :disabled="busy" @click="action('rollback')">
        {{ busy ? '提交中…' : '确认回滚到部署前版本' }}
      </button>
    </footer>
    <footer v-else-if="cancelableStatuses.has(run.status)" class="deployment-actions compact">
      <button class="btn btn-danger btn-small" data-action="cancel" type="button" :disabled="busy" @click="action('cancel')">
        {{ busy ? '停止中…' : '取消部署' }}
      </button>
    </footer>
  </article>
</template>

<style scoped>
.deployment-run-card { --run-color: var(--accent); width: min(100%,860px); align-self: flex-start; overflow: hidden; border: 1px solid rgba(24,184,231,.26); border-radius: 12px; background: var(--run-surface); box-shadow: var(--shadow); }
.deployment-run-card.tone-success { --run-color: var(--success); border-color: rgba(54,217,149,.26); }
.deployment-run-card.tone-warning { --run-color: var(--warning); border-color: rgba(241,187,97,.3); }
.deployment-run-card.tone-danger { --run-color: var(--danger); border-color: rgba(255,112,111,.34); }
.deployment-run-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 15px 16px 13px; border-bottom: 1px solid var(--divider-soft); }
.deployment-title { min-width: 0; }
.deployment-eyebrow { color: var(--text-muted); font-size: 9.5px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase; }
.deployment-title h3 { overflow: hidden; margin: 4px 0 0; color: var(--text-primary); font-size: 15px; font-weight: 720; text-overflow: ellipsis; white-space: nowrap; }
.deployment-status { flex: 0 0 auto; display: inline-flex; align-items: center; gap: 7px; padding: 5px 9px; border: 1px solid color-mix(in srgb,var(--run-color) 38%,transparent); border-radius: 999px; color: var(--run-color); font-size: 10.5px; font-weight: 650; }
.deployment-status-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; animation: deployment-pulse 1.25s ease-in-out infinite; }
@keyframes deployment-pulse { 50% { opacity: .35; } }
.deployment-meta { display: flex; flex-wrap: wrap; gap: 8px 18px; padding: 11px 16px; color: var(--text-secondary); font-size: 11px; }
.deployment-meta span { display: inline-flex; gap: 6px; }
.deployment-meta small { color: var(--text-muted); }
.deployment-progress { height: 2px; overflow: hidden; background: var(--surface-hover); }
.deployment-progress > span { display: block; height: 100%; background: var(--run-color); transition: width .25s ease; }
.deployment-plan-notice,.deployment-critical { margin: 13px 16px 0; padding: 10px 11px; border-left: 2px solid var(--run-color); border-radius: 0 7px 7px 0; background: color-mix(in srgb,var(--run-color) 9%,transparent); }
.deployment-plan-notice strong,.deployment-critical strong { font-size: 12px; }
.deployment-plan-notice p,.deployment-critical p { margin: 4px 0; color: var(--text-secondary); font-size: 11px; line-height: 1.55; }
.deployment-plan-notice > span { color: var(--text-muted); font: 9.5px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; }
.deployment-steps { display: grid; gap: 0; margin: 0; padding: 13px 16px 4px; list-style: none; }
.deployment-step { position: relative; min-height: 42px; display: grid; grid-template-columns: 24px minmax(0,1fr); gap: 9px; padding-bottom: 11px; }
.deployment-step:not(:last-child)::before { content: ""; position: absolute; top: 20px; bottom: -1px; left: 10px; border-left: 1px solid var(--border); }
.deployment-step-marker { position: relative; z-index: 1; width: 21px; height: 21px; display: grid; place-items: center; border: 1px solid var(--border-light); border-radius: 50%; background: var(--bg-secondary); color: var(--text-muted); font-size: 9px; }
.step-success .deployment-step-marker { border-color: rgba(54,217,149,.4); background: rgba(54,217,149,.12); color: var(--success); }
.step-running .deployment-step-marker { border-color: rgba(241,187,97,.5); color: var(--warning); box-shadow: 0 0 0 3px rgba(241,187,97,.08); }
.step-failed .deployment-step-marker,.step-unknown .deployment-step-marker { border-color: rgba(255,112,111,.5); color: var(--danger); }
.deployment-step-copy { min-width: 0; display: grid; gap: 3px; padding-top: 1px; }
.deployment-step-copy > div { display: flex; align-items: baseline; gap: 8px; }
.deployment-step-copy strong { color: var(--text-secondary); font-size: 11.5px; font-weight: 610; }
.deployment-step.current .deployment-step-copy strong { color: var(--text-primary); }
.deployment-step-copy > div span { color: var(--text-muted); font-size: 9px; }
.deployment-step-copy small { color: var(--text-muted); font-size: 9.5px; }
.deployment-step-copy details { color: var(--text-muted); font-size: 10px; }
.deployment-step-copy summary { width: fit-content; cursor: pointer; }
.deployment-step-copy pre { max-height: 180px; margin: 5px 0 0; padding: 7px 8px; overflow: auto; border: 1px solid var(--border); border-radius: 5px; background: var(--terminal-bg); color: var(--text-secondary); font: 10px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
.deployment-rollback-plan { margin: 0 16px 13px 49px; color: var(--text-muted); font-size: 10.5px; }
.deployment-rollback-plan summary { width: fit-content; cursor: pointer; }
.deployment-rollback-plan ol { margin: 7px 0 0; padding-left: 18px; }
.deployment-rollback-flow { margin: 0 16px 13px; padding-top: 10px; border-top: 1px solid var(--divider-soft); }
.deployment-rollback-flow > strong { color: var(--text-secondary); font-size: 11px; }
.deployment-rollback-flow .deployment-steps { padding: 9px 0 0; }
.deployment-error { margin: 0 16px 13px; padding: 8px 10px; border-radius: 6px; background: var(--danger-soft); color: var(--danger-text); font-size: 11px; line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere; }
.deployment-result { margin: 0; padding: 10px 16px 13px; border-top: 1px solid var(--divider-soft); color: var(--text-secondary); font-size: 11.5px; line-height: 1.55; }
.deployment-actions { display: flex; justify-content: flex-start; gap: 8px; padding: 11px 16px; border-top: 1px solid var(--divider-soft); background: var(--surface-soft); }
.deployment-actions.compact { padding-top: 8px; padding-bottom: 8px; }
@media (max-width: 700px) { .deployment-run-header { align-items: stretch; flex-direction: column; } .deployment-status { align-self: flex-start; } .deployment-meta { display: grid; grid-template-columns: 1fr 1fr; } }
</style>
