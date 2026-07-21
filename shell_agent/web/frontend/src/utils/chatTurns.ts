import type {
  CommandPreviewEvent,
  ExecutionResultEvent,
  ExecutionStatusEvent,
  ServerEvent,
  TaskStepEvent,
} from '../api/protocol'
import type { TimelineEntry } from '../stores/chat'

export interface ExecutionStepItem {
  kind: 'execution'
  id: string
  preview?: CommandPreviewEvent
  result?: ExecutionResultEvent
  status?: ExecutionStatusEvent
  taskStep?: TaskStepEvent
}

export interface EventItem {
  kind: 'event'
  id: string
  entry: TimelineEntry
}

export type TurnRenderItem = ExecutionStepItem | EventItem

export interface ChatTurnSummary {
  title: string
  detail: string
  status: 'success' | 'warning' | 'danger' | 'neutral'
  statusLabel: string
  chips: string[]
}

export interface ChatTurn {
  id: string
  user?: TimelineEntry
  entries: TimelineEntry[]
  items: TurnRenderItem[]
  summary: ChatTurnSummary
}

function text(event: ServerEvent | undefined, key: string): string {
  const value = event?.[key]
  return typeof value === 'string' ? value.trim() : ''
}

function compact(value: string, limit: number): string {
  const normalized = value.replace(/\s+/g, ' ').trim()
  return normalized.length > limit ? `${normalized.slice(0, limit - 1)}…` : normalized
}

function turnId(entry: TimelineEntry, fallback: string): string {
  const event = entry.event
  return text(event, 'turn_id') || fallback
}

function signature(event: ServerEvent | undefined): string {
  const command = text(event, 'command')
  if (!command) return ''
  return `${command}\u0000${text(event, 'target')}`
}

function isTransientEvent(event: ServerEvent): boolean {
  if (event.type === 'turn_state') return true
  if (event.type === 'confirm_prompt') return true
  if (event.type !== 'system') return false
  return /^(正在|开始)(生成命令|分析|处理|执行)/.test(text(event, 'content'))
}

export function buildTurnItems(entries: TimelineEntry[]): TurnRenderItem[] {
  const items: TurnRenderItem[] = []
  const steps: ExecutionStepItem[] = []

  const stepSignature = (step: ExecutionStepItem): string => (
    signature(step.preview) || signature(step.taskStep) || signature(step.result)
  )

  const findStep = (event: ServerEvent, requireOpenResult = false): ExecutionStepItem | undefined => {
    const eventSignature = signature(event)
    const eventIndex = Number(event.step_index ?? 0)
    return [...steps].reverse().find((candidate) => {
      if (requireOpenResult && candidate.result) return false
      const candidateSignature = stepSignature(candidate)
      if (eventSignature && candidateSignature) return eventSignature === candidateSignature
      const candidateIndex = Number(candidate.taskStep?.step_index ?? 0)
      return Boolean(eventIndex && candidateIndex && eventIndex === candidateIndex)
    })
  }

  for (const entry of entries) {
    const event = entry.event
    if (event.type === 'task_step' && text(event, 'status') !== 'complete') {
      const taskStep = event as TaskStepEvent
      const step = findStep(taskStep) ?? {
        kind: 'execution' as const,
        id: `execution-step-${text(taskStep, 'step_index') || entry.id}`,
      }
      step.taskStep = taskStep
      if (!steps.includes(step)) {
        steps.push(step)
        items.push(step)
      }
      continue
    }

    if (event.type === 'command_preview') {
      const preview = event as CommandPreviewEvent
      const step = findStep(preview) ?? {
        kind: 'execution' as const,
        id: `execution-${entry.id}`,
      }
      step.preview = preview
      if (!steps.includes(step)) {
        steps.push(step)
        items.push(step)
      }
      continue
    }

    if (event.type === 'execution_status') {
      const taskId = text(event, 'task_id')
      const step = [...steps].reverse().find((candidate) => {
        if (candidate.result) return false
        const previewTask = text(candidate.preview, 'task_id')
        return !taskId || !previewTask || taskId === previewTask
      })
      if (step) step.status = event as ExecutionStatusEvent
      continue
    }

    if (event.type === 'execution_result') {
      const result = event as ExecutionResultEvent
      const taskId = text(result, 'task_id')
      const step = findStep(result, true) ?? [...steps].reverse().find((candidate) => {
        if (candidate.result) return false
        const candidateTaskId = text(candidate.preview, 'task_id') || text(candidate.taskStep, 'task_id')
        return Boolean(taskId && candidateTaskId && taskId === candidateTaskId)
      }) ?? [...steps].reverse().find((candidate) => !candidate.result)

      if (step) {
        step.result = result
      } else {
        const resultOnlyStep: ExecutionStepItem = {
          kind: 'execution',
          id: `execution-${entry.id}`,
          result,
        }
        steps.push(resultOnlyStep)
        items.push(resultOnlyStep)
      }
      continue
    }

    if (!isTransientEvent(event)) {
      items.push({ kind: 'event', id: entry.id, entry })
    }
  }

  return items
}

function summarizeTurn(user: TimelineEntry | undefined, entries: TimelineEntry[]): ChatTurnSummary {
  const title = compact(text(user?.event, 'content') || '会话事件', 72)
  const latestAgent = [...entries].reverse().find((entry) => entry.event.type === 'agent')
  const finalStep = [...entries].reverse().find((entry) => (
    entry.event.type === 'task_step' && text(entry.event, 'status') === 'complete'
  ))
  const latestState = [...entries].reverse().find((entry) => entry.event.type === 'turn_state')
  const results = entries.filter((entry) => entry.event.type === 'execution_result')
  const failedResult = results.find((entry) => !Boolean(entry.event.success) && !Boolean(entry.event.partial_success))
  const partialResult = results.find((entry) => Boolean(entry.event.partial_success))
  const state = text(latestState?.event, 'status')

  let status: ChatTurnSummary['status'] = 'neutral'
  let statusLabel = '已回复'
  if (failedResult || ['failed', 'blocked', 'timeout', 'canceled'].includes(state)) {
    status = 'danger'
    statusLabel = state === 'timeout' ? '已超时' : state === 'canceled' ? '已取消' : '执行失败'
  } else if (partialResult || ['waiting_confirm', 'thinking', 'planning', 'executing', 'running'].includes(state)) {
    status = 'warning'
    statusLabel = text(latestState?.event, 'label') || (partialResult ? '部分完成' : '处理中')
  } else if (results.length || finalStep || state === 'completed') {
    status = 'success'
    statusLabel = '已完成'
  }

  const detailSource = text(finalStep?.event, 'content') || text(latestAgent?.event, 'content')
  const targets = entries.map((entry) => text(entry.event, 'target')).filter(Boolean)
  const risks = entries.map((entry) => text(entry.event, 'risk_level')).filter(Boolean)
  const chips = [...new Set([...targets, ...risks])].slice(0, 4)

  return {
    title,
    detail: compact(detailSource || 'Opsane 正在整理本回合的处理结果。', 150),
    status,
    statusLabel,
    chips,
  }
}

export function groupChatTurns(entries: TimelineEntry[]): ChatTurn[] {
  const turns: ChatTurn[] = []
  const byId = new Map<string, ChatTurn>()
  let current: ChatTurn | undefined

  for (const entry of entries) {
    const event = entry.event
    if (event.type === 'user_message') {
      const id = turnId(entry, `turn-${entry.id}`)
      current = byId.get(id)
      if (!current) {
        current = { id, user: entry, entries: [], items: [], summary: summarizeTurn(entry, []) }
        turns.push(current)
        byId.set(id, current)
      } else {
        current.user = entry
      }
      continue
    }

    const explicitId = text(event, 'turn_id')
    if (explicitId && byId.has(explicitId)) current = byId.get(explicitId)
    if (!current) {
      const id = explicitId || `turn-${entry.id}`
      current = { id, entries: [], items: [], summary: summarizeTurn(undefined, []) }
      turns.push(current)
      byId.set(id, current)
    }
    current.entries.push(entry)
  }

  for (const turn of turns) {
    turn.items = buildTurnItems(turn.entries)
    turn.summary = summarizeTurn(turn.user, turn.entries)
  }
  return turns
}
