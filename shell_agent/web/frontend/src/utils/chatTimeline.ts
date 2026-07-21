import type { DeploymentRunRecord } from '../api/deployments'
import type { TimelineEntry } from '../stores/chat'
import type { ChatTurn } from './chatTurns'

export type ChatTimelineProjectionItem =
  | { kind: 'turn'; id: string; turn: ChatTurn }
  | { kind: 'deployment'; id: string; run: DeploymentRunRecord }

function parsedTimestamp(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value !== 'string' || !value.trim()) return undefined
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : undefined
}

function entryTimestamp(entry: TimelineEntry | undefined): number | undefined {
  if (!entry) return undefined
  return entry.receivedAt ?? parsedTimestamp(entry.event.timestamp)
}

function turnTimestamp(turn: ChatTurn): number | undefined {
  return entryTimestamp(turn.user) ?? entryTimestamp(turn.entries[0])
}

/**
 * Projects the active deployment into the conversation at its creation time.
 * The deployment remains a live singleton in the store, but no longer floats
 * below later chat turns merely because it is rendered outside the turn loop.
 */
export function projectChatTimeline(
  turns: ChatTurn[],
  deployment: DeploymentRunRecord | null | undefined,
): ChatTimelineProjectionItem[] {
  const projected: ChatTimelineProjectionItem[] = turns.map((turn) => ({
    kind: 'turn',
    id: `turn:${turn.id}`,
    turn,
  }))
  if (!deployment) return projected

  const deploymentTime = parsedTimestamp(deployment.created_at)
  let insertionIndex = projected.length
  if (deploymentTime !== undefined) {
    const firstLaterTurn = turns.findIndex((turn) => {
      const timestamp = turnTimestamp(turn)
      return timestamp !== undefined && timestamp > deploymentTime
    })
    if (firstLaterTurn >= 0) insertionIndex = firstLaterTurn
  }

  projected.splice(insertionIndex, 0, {
    kind: 'deployment',
    id: `deployment:${deployment.id}`,
    run: deployment,
  })
  return projected
}
