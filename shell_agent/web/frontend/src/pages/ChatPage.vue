<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import type { CommandPreviewEvent, ConfirmMode, ServerEvent } from '../api/protocol'
import { errorMessage } from '../api/http'
import DeploymentRunCard from '../components/chat/DeploymentRunCard.vue'
import ExecutionStep from '../components/chat/ExecutionStep.vue'
import ChatTimelineItem from '../components/chat/ChatTimelineItem.vue'
import SessionFileTransferDialog from '../components/chat/SessionFileTransferDialog.vue'
import SessionListItem from '../components/common/SessionListItem.vue'
import { useChatDraftsStore } from '../stores/chatDrafts'
import { useChatStore } from '../stores/chat'
import { useDeploymentsStore } from '../stores/deployments'
import { useInventoryStore } from '../stores/inventory'
import { useNotificationsStore } from '../stores/notifications'
import {
  type SessionFileRecord,
  type SessionFileTransferInput,
  useSessionFilesStore,
} from '../stores/sessionFiles'
import { useSessionsStore } from '../stores/sessions'
import { groupChatTurns, type ChatTurn } from '../utils/chatTurns'
import { projectChatTimeline } from '../utils/chatTimeline'
import { confirmAction } from '../utils/confirm'
import { formatCompactDateTime } from '../utils/dateTime'
import { filesFromTransfer, transferHasFiles } from '../utils/transferFiles'

const chat = useChatStore()
const chatDrafts = useChatDraftsStore()
const deployments = useDeploymentsStore()
const sessions = useSessionsStore()
const sessionFiles = useSessionFilesStore()
const inventory = useInventoryStore()
const notifications = useNotificationsStore()
const input = computed<string>({
  get: () => chatDrafts.forSession(chat.sessionId),
  set: (draft) => chatDrafts.setDraft(chat.sessionId, draft),
})
const composerInput = ref<HTMLTextAreaElement | null>(null)
const composerWrap = ref<HTMLElement | null>(null)
const composerClearance = ref(172)
const search = ref('')
const timeline = ref<HTMLElement | null>(null)
const loadingSession = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const composerDropActive = ref(false)
const filePanelOpen = ref(true)
const filePanelCollapsed = ref(false)
const previewFile = ref<SessionFileRecord | null>(null)
const previewText = ref('')
const previewLoading = ref(false)
const previewMode = ref<'formatted' | 'text'>('formatted')
const previewError = ref('')
const transferDialogFile = ref<SessionFileRecord | null>(null)
const timelineRail = ref<HTMLElement | null>(null)
const hoveredTurnId = ref('')
const turnPositions = ref<Record<string, number>>({})
const viewportIndicator = ref({ top: 18, height: 16 })
const summaryCardTop = ref(12)
const turnNodes = new Map<string, HTMLElement>()
interface SessionScrollState {
  atBottom: boolean
  scrollTop: number
  anchorTurnId?: string
  anchorOffset?: number
}
const sessionScrollStates = new Map<string, SessionScrollState>()
const stickToBottom = ref(true)
const restoringSessionScroll = ref(false)
let geometryFrame = 0
let composerResizeObserver: ResizeObserver | null = null
let transferPollTimer: ReturnType<typeof setTimeout> | null = null
let composerDragDepth = 0
let previewRequestVersion = 0

const officePreviewExtensions = new Set(['.doc', '.docx', '.xls', '.xlsx', '.xlsm', '.ppt', '.pptx'])

const turns = computed(() => groupChatTurns(chat.entries))
const timelineItems = computed(() => projectChatTimeline(turns.value, deployments.run))
const hoveredTurn = computed(() => turns.value.find((turn) => turn.id === hoveredTurnId.value))
const selectedConfirmMode = computed<ConfirmMode>({
  get: () => chat.confirmMode,
  set: (mode) => chat.setConfirmMode(mode),
})
const fullAccess = computed(() => chat.confirmMode === 'full_access')
const composerUnavailable = computed(() => (
  loadingSession.value
  || restoringSessionScroll.value
  || deployments.loading
  || !chat.sessionId
  || chat.connectionState !== 'open'
))
const conversationLocked = computed(() => (
  chat.busy
  || deployments.locksComposer
  || deployments.pendingAction === 'create'
))
const fileUploadUnavailable = computed(() => (
  composerUnavailable.value
  || conversationLocked.value
  || sessionFiles.uploading
))
const composerPlaceholder = computed(() => deployments.locksComposer
  ? '部署任务进行中，完成或安全回滚后可继续对话…'
  : '描述你想完成的任务…')
const activeTaskState = computed(() => Object.values(chat.turnStates).filter((state) => state.active).at(-1))
const activeTaskLabel = computed(() => activeTaskState.value?.label || 'Agent 正在处理当前任务')
const activeTaskStatus = computed(() => String(activeTaskState.value?.status || 'running'))
const taskStopping = computed(() => activeTaskStatus.value === 'stopping')
const parsedFileCount = computed(() => sessionFiles.items.filter((file) => (
  file.parse_status === 'ready' || file.parse_status === 'metadata_only'
)).length)
const totalFileSize = computed(() => sessionFiles.items.reduce((total, file) => total + (Number(file.size) || 0), 0))
const filePanelSummary = computed(() => {
  if (sessionFiles.uploading) return '正在上传并解析'
  if (!sessionFiles.items.length) return '还没有文件'
  return `${parsedFileCount.value} 已解析 · ${formatBytes(totalFileSize.value)}`
})

const connectionLabel = computed(() => ({
  idle: '未连接', connecting: '连接中', open: '已连接', reconnecting: '重连中', closed: '已断开', error: '连接异常',
}[chat.connectionState]))

function isPreviewActionable(event: ServerEvent): boolean {
  return event.type === 'command_preview'
    && Boolean(chat.pendingPreview)
    && event.task_id === chat.pendingPreview?.task_id
}

function isPlanActionable(event: ServerEvent): boolean {
  return event.type === 'operation_plan'
    && Boolean(chat.activePlan)
    && event.plan_id === chat.activePlan?.plan_id
}

function fileTransferId(event: ServerEvent): string {
  if (event.type !== 'file_transfer_preview' || !event.transfer || typeof event.transfer !== 'object') return ''
  return String((event.transfer as Record<string, unknown>).id ?? '')
}

function isFileTransferActionable(event: ServerEvent): boolean {
  if (event.type !== 'file_transfer_preview' || !chat.pendingFileTransfer) return false
  return Boolean(fileTransferId(event) && fileTransferId(event) === String(chat.pendingFileTransfer.transfer.id ?? ''))
}

function isFileTransferSubmitting(event: ServerEvent): boolean {
  return Boolean(fileTransferId(event) && fileTransferId(event) === chat.confirmingFileTransferId)
}

function fileTransferDisplayStatus(event: ServerEvent, turn: ChatTurn): string {
  const transferId = fileTransferId(event)
  if (!transferId) return ''

  const artifactEvent = [...chat.entries].reverse().find((entry) => {
    if (entry.event.type !== 'artifact_upload' || !entry.event.artifact || typeof entry.event.artifact !== 'object') return false
    const artifact = entry.event.artifact as Record<string, unknown>
    return String(artifact.transfer_id ?? artifact.id ?? '') === transferId
  })?.event
  if (artifactEvent?.artifact && typeof artifactEvent.artifact === 'object') {
    const artifact = artifactEvent.artifact as Record<string, unknown>
    const artifactStatus = String(artifact.status ?? '')
    if (artifactStatus) return artifactStatus
    return artifact.error ? 'failed' : 'success'
  }

  const transferState = [...turn.entries].reverse().find((entry) => (
    entry.event.type === 'turn_state'
    && String(entry.event.transfer_id ?? '') === transferId
  ))?.event
  if (transferState?.status) return String(transferState.status)

  return String(sessionFiles.transfers.find((transfer) => transfer.id === transferId)?.status ?? '')
}

function operationPlanStatus(event: ServerEvent, turn: ChatTurn): 'waiting' | 'confirmed' | 'canceled' | 'archived' {
  if (isPlanActionable(event)) return 'waiting'
  const latestState = [...turn.entries].reverse().find((entry) => entry.event.type === 'turn_state')?.event
  if (latestState && ['canceled', 'blocked'].includes(String(latestState.status ?? ''))) return 'canceled'
  const executionStarted = turn.entries.some((entry) => (
    entry.event.type === 'command_preview'
    || entry.event.type === 'execution_result'
    || (entry.event.type === 'task_step' && String(entry.event.status ?? '') !== 'complete')
  ))
  if (executionStarted || latestState && ['thinking', 'executing', 'running', 'completed'].includes(String(latestState.status ?? ''))) {
    return 'confirmed'
  }
  return event.active === false ? 'archived' : 'confirmed'
}

function isExecutionActionable(preview?: CommandPreviewEvent): boolean {
  return Boolean(preview && isPreviewActionable(preview))
}

function setTurnNode(id: string, element: unknown) {
  if (element instanceof HTMLElement) turnNodes.set(id, element)
  else turnNodes.delete(id)
  scheduleTurnGeometry()
}

function updateTurnGeometry() {
  geometryFrame = 0
  const scroller = timeline.value
  const rail = timelineRail.value
  if (!scroller || !rail) return
  const edge = 18
  const usable = Math.max(1, rail.clientHeight - edge * 2)
  const contentHeight = Math.max(scroller.scrollHeight, scroller.clientHeight, 1)
  const positions: Record<string, number> = {}
  for (const turn of turns.value) {
    const node = turnNodes.get(turn.id)
    if (!node) continue
    const anchor = node.offsetTop + Math.min(Math.max(node.offsetHeight * .28, 20), 110)
    positions[turn.id] = edge + Math.max(0, Math.min(1, anchor / contentHeight)) * usable
  }
  turnPositions.value = positions

  const viewportHeight = Math.max(16, usable * Math.min(1, scroller.clientHeight / contentHeight))
  const maxScroll = Math.max(1, contentHeight - scroller.clientHeight)
  const travel = Math.max(0, usable - viewportHeight)
  viewportIndicator.value = {
    top: edge + travel * Math.max(0, Math.min(1, scroller.scrollTop / maxScroll)),
    height: viewportHeight,
  }
}

function scheduleTurnGeometry() {
  if (geometryFrame) return
  geometryFrame = requestAnimationFrame(updateTurnGeometry)
}

function nearestTurn(pointerY: number): ChatTurn | undefined {
  return turns.value.reduce<ChatTurn | undefined>((nearest, turn) => {
    const position = turnPositions.value[turn.id]
    if (position === undefined) return nearest
    if (!nearest) return turn
    return Math.abs(position - pointerY) < Math.abs((turnPositions.value[nearest.id] ?? 0) - pointerY)
      ? turn
      : nearest
  }, undefined)
}

function showTurnSummary(turn: ChatTurn, pointerY: number) {
  hoveredTurnId.value = turn.id
  const railHeight = timelineRail.value?.clientHeight ?? 0
  summaryCardTop.value = Math.max(8, Math.min(Math.max(8, railHeight - 176), pointerY - 62))
}

function handleTimelineMove(event: MouseEvent) {
  const rail = timelineRail.value
  if (!rail) return
  const y = event.clientY - rail.getBoundingClientRect().top
  const turn = nearestTurn(y)
  if (turn) showTurnSummary(turn, y)
}

function jumpToTurn(turn: ChatTurn) {
  const scroller = timeline.value
  const node = turnNodes.get(turn.id)
  if (!scroller || !node) return
  hoveredTurnId.value = ''
  scroller.scrollTo({
    top: Math.max(0, Math.min(scroller.scrollHeight - scroller.clientHeight, node.offsetTop - 18)),
    behavior: 'smooth',
  })
}

function handleTimelineClick(event: MouseEvent) {
  const rail = timelineRail.value
  if (!rail) return
  const turn = nearestTurn(event.clientY - rail.getBoundingClientRect().top)
  if (turn) jumpToTurn(turn)
}

function handleTimelineWheel(event: WheelEvent) {
  if (!timeline.value) return
  timeline.value.scrollTop += event.deltaY
}

async function selectSession(id: string) {
  captureSessionScrollState(chat.sessionId)
  stopTransferPolling()
  transferDialogFile.value = null
  restoringSessionScroll.value = true
  loadingSession.value = true
  closeFilePreview()
  try {
    const detail = await sessions.select(id, 200)
    chat.setSession(id)
    chat.hydrate(detail.messages)
    chat.restoreTasks(detail.tasks)
    chat.restorePending(detail.pending)
    await Promise.all([
      sessionFiles.load(id),
      restoreDeploymentRun(id, detail as Record<string, unknown>),
    ])
    deployments.startPolling(id)
    scheduleTransferPolling()
  } finally {
    loadingSession.value = false
    await restoreSessionScrollPosition(id)
  }
}

function deploymentRunIdFromSession(detail: Record<string, unknown>): string {
  const direct = detail.active_deployment_run_id ?? detail.deployment_run_id
  if (typeof direct === 'string' && direct) return direct
  if (Array.isArray(detail.deployment_runs)) {
    const run = detail.deployment_runs.find((item) => item && typeof item === 'object') as Record<string, unknown> | undefined
    if (typeof run?.id === 'string') return run.id
    if (typeof run?.run_id === 'string') return run.run_id
  }
  if (Array.isArray(detail.messages)) {
    for (const message of [...detail.messages].reverse()) {
      if (!message || typeof message !== 'object') continue
      const value = message as Record<string, unknown>
      const payload = value.payload && typeof value.payload === 'object'
        ? value.payload as Record<string, unknown>
        : {}
      const runId = payload.run_id ?? payload.deployment_run_id
      if (typeof runId === 'string' && runId) return runId
    }
  }
  return ''
}

async function restoreDeploymentRun(sessionId: string, detail: Record<string, unknown>) {
  const runId = deploymentRunIdFromSession(detail)
  return runId
    ? deployments.adoptRun(sessionId, runId)
    : deployments.loadSession(sessionId)
}

async function scrollTimelineToBottom() {
  await nextTick()
  const scroller = timeline.value
  if (!scroller) return
  scroller.scrollTop = scroller.scrollHeight
  stickToBottom.value = true
  scheduleTurnGeometry()
}

function captureSessionScrollState(sessionId: string) {
  if (!sessionId || !timeline.value) return
  const scroller = timeline.value
  const maxScroll = Math.max(0, scroller.scrollHeight - scroller.clientHeight)
  const atBottom = maxScroll - scroller.scrollTop < 80
  const state: SessionScrollState = {
    atBottom,
    scrollTop: scroller.scrollTop,
  }
  if (!atBottom) {
    const anchor = turns.value.find((turn) => {
      const node = turnNodes.get(turn.id)
      return Boolean(node && node.offsetTop + node.offsetHeight > scroller.scrollTop + 8)
    })
    const anchorNode = anchor ? turnNodes.get(anchor.id) : undefined
    if (anchor && anchorNode) {
      state.anchorTurnId = anchor.id
      state.anchorOffset = anchorNode.offsetTop - scroller.scrollTop
    }
  }
  sessionScrollStates.set(sessionId, state)
}

async function restoreSessionScrollPosition(sessionId: string) {
  await nextTick()
  const scroller = timeline.value
  if (!scroller) {
    restoringSessionScroll.value = false
    return
  }
  const maxScroll = Math.max(0, scroller.scrollHeight - scroller.clientHeight)
  const state = sessionScrollStates.get(sessionId)
  let targetTop = maxScroll
  if (state && !state.atBottom) {
    const anchorNode = state.anchorTurnId ? turnNodes.get(state.anchorTurnId) : undefined
    targetTop = anchorNode
      ? anchorNode.offsetTop - (state.anchorOffset ?? 0)
      : state.scrollTop
  }
  scroller.scrollTop = Math.max(0, Math.min(maxScroll, targetTop))
  stickToBottom.value = state?.atBottom ?? true
  scheduleTurnGeometry()
  await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
  restoringSessionScroll.value = false
}

function handleChatTimelineScroll() {
  const scroller = timeline.value
  if (!scroller) return
  if (loadingSession.value || restoringSessionScroll.value) {
    scheduleTurnGeometry()
    return
  }
  const maxScroll = Math.max(0, scroller.scrollHeight - scroller.clientHeight)
  stickToBottom.value = maxScroll - scroller.scrollTop < 80
  captureSessionScrollState(chat.sessionId)
  scheduleTurnGeometry()
}

async function loadSessions() {
  await sessions.load('chat', search.value)
  if (chat.sessionId && sessions.items.some((item) => item.id === chat.sessionId)) return
  if (sessions.items[0]) {
    await selectSession(sessions.items[0].id)
  } else if (!search.value) {
    await createSession()
  }
}

async function createSession() {
  loadingSession.value = true
  closeFilePreview()
  stopTransferPolling()
  transferDialogFile.value = null
  try {
    const session = await sessions.create('chat')
    chat.setSession(session.id)
    chat.hydrate(sessions.selected?.messages)
    chat.restoreTasks(sessions.selected?.tasks)
    chat.restorePending(sessions.selected?.pending)
    await Promise.all([
      sessionFiles.load(session.id),
      restoreDeploymentRun(session.id, (sessions.selected ?? {}) as Record<string, unknown>),
    ])
    deployments.startPolling(session.id)
    scheduleTransferPolling()
  } finally {
    loadingSession.value = false
  }
}

async function removeSession(id: string) {
  if (!confirmAction('确定删除这个聊天会话吗？')) return
  try {
    await sessions.remove(id)
    chatDrafts.clearDraft(id)
    notifications.success('聊天会话已删除')
    if (chat.sessionId === id) {
      chat.setSession('')
      sessionFiles.reset()
      deployments.reset()
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
    await sessions.load('chat', search.value)
    notifications.success(pinned ? '会话已置顶' : '已取消置顶')
  } catch (error) {
    notifications.error(errorMessage(error))
  }
}

async function uploadSessionFiles(files: File[]) {
  if (!files.length) return
  const uploadSessionId = chat.sessionId
  if (!uploadSessionId || fileUploadUnavailable.value) {
    notifications.error('当前会话暂时无法接收文件，请等待当前任务完成')
    return
  }
  try {
    const uploaded = await sessionFiles.upload(uploadSessionId, files)
    if (chat.sessionId !== uploadSessionId) return
    filePanelOpen.value = true
    filePanelCollapsed.value = false
    notifications.success(`已上传 ${uploaded.length} 个文件`)
  } catch (error) {
    if (chat.sessionId !== uploadSessionId) return
    notifications.error(errorMessage(error))
  }
}

function uploadFiles(event: Event) {
  const element = event.target as HTMLInputElement
  const files = Array.from(element.files ?? [])
  element.value = ''
  void uploadSessionFiles(files)
}

function handleComposerPaste(event: ClipboardEvent) {
  const files = filesFromTransfer(event.clipboardData)
  if (!files.length) return
  event.preventDefault()
  void uploadSessionFiles(files)
}

function resetComposerDragState() {
  composerDragDepth = 0
  composerDropActive.value = false
}

function handleComposerDragEnter(event: DragEvent) {
  if (!transferHasFiles(event.dataTransfer)) return
  event.preventDefault()
  composerDragDepth += 1
  composerDropActive.value = !fileUploadUnavailable.value
}

function handleComposerDragOver(event: DragEvent) {
  if (!transferHasFiles(event.dataTransfer)) return
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = fileUploadUnavailable.value ? 'none' : 'copy'
}

function handleComposerDragLeave() {
  if (!composerDragDepth) return
  composerDragDepth = Math.max(0, composerDragDepth - 1)
  if (!composerDragDepth) composerDropActive.value = false
}

function handleComposerDrop(event: DragEvent) {
  if (!transferHasFiles(event.dataTransfer)) return
  event.preventDefault()
  const files = filesFromTransfer(event.dataTransfer)
  resetComposerDragState()
  void uploadSessionFiles(files)
}

function isOfficePreviewFile(file: SessionFileRecord): boolean {
  return officePreviewExtensions.has(String(file.extension || '').toLowerCase())
}

function closeFilePreview() {
  previewRequestVersion += 1
  previewFile.value = null
  previewText.value = ''
  previewError.value = ''
  previewLoading.value = false
  previewMode.value = 'formatted'
}

function isCurrentPreviewRequest(version: number, fileId: string, sessionId: string): boolean {
  return version === previewRequestVersion
    && previewFile.value?.id === fileId
    && chat.sessionId === sessionId
}

async function openFilePreview(file: SessionFileRecord) {
  const version = ++previewRequestVersion
  const sessionId = file.session_id || chat.sessionId
  const officeFile = isOfficePreviewFile(file)
  previewFile.value = file
  previewText.value = ''
  previewError.value = ''
  previewMode.value = officeFile || file.preview_type === 'pdf' ? 'formatted' : 'text'
  previewLoading.value = true

  const contentRequest = (officeFile || ['text', 'image', 'pdf'].includes(file.preview_type))
    ? sessionFiles.content(file).then(
      (data) => ({ data, error: '' }),
      (error) => ({ data: null, error: errorMessage(error) }),
    )
    : Promise.resolve({ data: null, error: '' })
  const renderRequest = officeFile
    ? sessionFiles.renderPreview(file).then(
      (rendered) => ({ rendered, error: '' }),
      (error) => ({ rendered: file, error: errorMessage(error) }),
    )
    : Promise.resolve({ rendered: file, error: '' })

  const contentResult = await contentRequest
  if (!isCurrentPreviewRequest(version, file.id, sessionId)) return

  const content = contentResult.data?.content || ''
  const contentFallback = contentResult.data?.parse_error || contentResult.error
  previewText.value = content
  if (!content && contentFallback) previewError.value = contentFallback

  const renderResult = await renderRequest
  if (!isCurrentPreviewRequest(version, file.id, sessionId)) return

  const rendered = renderResult.rendered
  previewFile.value = rendered

  if (officeFile && rendered.preview_type !== 'pdf') {
    const renderError = rendered.layout_preview_error || renderResult.error || '版式预览生成失败'
    previewMode.value = 'text'
    previewError.value = renderError
    notifications.error(`${file.name} 无法生成版式预览，已回退到提取文本`)
  } else if (rendered.preview_type === 'text') {
    previewMode.value = 'text'
  }

  if (!previewText.value && rendered.preview_type === 'text') {
    previewText.value = contentFallback || previewError.value || '没有可预览的文本内容'
  } else if (!previewText.value && rendered.preview_type === 'image' && contentFallback) {
    previewText.value = contentFallback
  } else if (!previewText.value && rendered.preview_type === 'none') {
    previewError.value = previewError.value || contentFallback
  }
  previewLoading.value = false
}

async function removeFile(file: SessionFileRecord) {
  if (!confirmAction(`确定删除附件“${file.name}”吗？`)) return
  try {
    await sessionFiles.remove(file)
    if (previewFile.value?.id === file.id) closeFilePreview()
    notifications.success(`已删除 ${file.name}`)
  } catch (error) {
    notifications.error(errorMessage(error))
  }
}

function canReanalyzeLegacyFile(file: SessionFileRecord): boolean {
  const extension = String(file.extension || '').toLowerCase()
  return ['.doc', '.xls', '.ppt'].includes(extension)
    && ['metadata_only', 'unsupported', 'error'].includes(file.parse_status)
}

async function reanalyzeFile(file: SessionFileRecord) {
  try {
    const updated = await sessionFiles.reanalyze(file)
    if (previewFile.value?.id === file.id) previewFile.value = updated
    if (updated.parse_status === 'ready') {
      notifications.success(`${file.name} 内容识别完成`)
    } else {
      notifications.error(updated.parse_error || `${file.name} 未识别出可用内容`)
    }
  } catch (error) {
    notifications.error(errorMessage(error))
  }
}

function stopTransferPolling() {
  if (transferPollTimer) clearTimeout(transferPollTimer)
  transferPollTimer = null
}

function scheduleTransferPolling() {
  stopTransferPolling()
  if (!Object.values(sessionFiles.transferStates).some((state) => state.status === 'running')) return
  const sessionId = chat.sessionId
  transferPollTimer = setTimeout(async () => {
    transferPollTimer = null
    if (chat.sessionId !== sessionId) return
    const previousStates = Object.fromEntries(
      Object.entries(sessionFiles.transferStates).map(([fileId, state]) => [fileId, state.status]),
    )
    try {
      await sessionFiles.refreshTransfers(sessionId)
      for (const [fileId, state] of Object.entries(sessionFiles.transferStates)) {
        if (previousStates[fileId] !== 'running') continue
        if (state.status === 'success') {
          notifications.success(`已传到 ${state.result?.target}:${state.result?.remote_path}`)
        } else if (state.status === 'failed') {
          notifications.error(state.error || '文件传输失败')
        }
      }
      scheduleTransferPolling()
    } catch {
      if (chat.sessionId === sessionId) scheduleTransferPolling()
    }
  }, 900)
}

async function openFileTransfer(file: SessionFileRecord) {
  transferDialogFile.value = file
  const state = sessionFiles.transferStateForFile(file.id)
  if (state?.status === 'running') scheduleTransferPolling()
  if (!inventory.servers.length && !inventory.loading) {
    try {
      await inventory.load()
    } catch {
      // inventory store exposes the error in the dialog
    }
  }
}

function closeFileTransfer() {
  const file = transferDialogFile.value
  if (file && ['submitting', 'running'].includes(sessionFiles.transferStateForFile(file.id)?.status ?? '')) return
  transferDialogFile.value = null
}

async function transferFile(input: SessionFileTransferInput) {
  const file = transferDialogFile.value
  if (!file) return
  try {
    const result = await sessionFiles.transfer(file, input)
    const state = sessionFiles.transferStateForFile(file.id)
    if (state?.status === 'running') {
      scheduleTransferPolling()
    } else if (state?.status === 'success') {
      notifications.success(`已传到 ${result?.target}:${result?.remote_path}`)
    } else if (state?.status === 'failed') {
      notifications.error(state.error || '文件传输失败')
    }
  } catch (error) {
    notifications.error(errorMessage(error))
  }
}

function fileTransferActionLabel(file: SessionFileRecord): string {
  const status = sessionFiles.transferStateForFile(file.id)?.status
  if (status === 'submitting' || status === 'running') return '上传中'
  if (status === 'waiting_confirm') return '等待确认'
  if (status === 'success') return '传输结果'
  if (status === 'failed') return '重试传输'
  return '传到服务器'
}

function deployableArtifactType(file: SessionFileRecord): 'jar' | 'war' | '' {
  const extension = file.extension.toLowerCase()
  const name = file.name.toLowerCase()
  if (extension === '.jar' || name.endsWith('.jar')) return 'jar'
  if (extension === '.war' || name.endsWith('.war')) return 'war'
  return ''
}

function isDeployableArtifact(file: SessionFileRecord): boolean {
  return Boolean(deployableArtifactType(file))
}

function deploymentMatchKey(value: string): string {
  return value.toLowerCase().replace(/\.(?:jar|war)$/i, '').replace(/[^a-z0-9\u4e00-\u9fff]+/g, '')
}

async function startFileDeployment(file: SessionFileRecord) {
  const artifactType = deployableArtifactType(file)
  if (!chat.sessionId || !artifactType || deployments.pendingAction) return
  if (deployments.locksComposer) {
    notifications.error('当前会话已有部署任务，请先完成或安全回滚')
    return
  }
  try {
    if (!inventory.services.length) await inventory.load()
    const eligible = inventory.services.filter((service) => (
      service.verification_status === 'verified'
      && ['dev', 'test'].includes(String(service.env || ''))
      && Array.isArray(service.servers)
      && service.servers.length === 1
      && String(service.artifact_type || 'jar').toLowerCase() === artifactType
    ))
    const artifactKey = deploymentMatchKey(file.name).replace(/v?\d+(?:\d+)*/g, '')
    const matched = eligible.filter((service) => {
      const serviceKeys = [service.id, service.name].map((value) => deploymentMatchKey(String(value || '')))
      return serviceKeys.some((key) => key && (artifactKey.includes(key) || key.includes(artifactKey)))
    })
    // Do not deploy an unrelated artifact merely because only one service profile
    // exists. Automatic selection requires a unique artifact-name match.
    const service = matched.length === 1 ? matched[0] : null
    if (!service) {
      throw new Error(eligible.length
        ? '无法从文件名唯一匹配服务画像，请完善服务名称后重试'
        : `没有可用于部署 ${artifactType.toUpperCase()} 的已验证 dev/test 单机服务画像`)
    }
    const run = await deployments.create({
      session_id: chat.sessionId,
      service_id: service.id,
      file_id: file.id,
    })
    deployments.startPolling(chat.sessionId)
    if (run) {
      notifications.success(run.status === 'waiting_plan_confirm'
        ? '部署前检查通过，请确认冻结方案'
        : '部署任务已创建')
      await scrollTimelineToBottom()
    }
  } catch (error) {
    notifications.error(errorMessage(error))
  }
}

function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  const index = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1)
  return `${(size / 1024 ** index).toFixed(index === 0 ? 0 : 1)} ${units[index]}`
}

function fileKindLabel(file: SessionFileRecord): string {
  return ({
    text: '文本', pdf: 'PDF', document: '文档', spreadsheet: '表格',
    presentation: '演示', image: '图片', archive: '压缩包', package: '安装包', other: '文件',
  } as Record<string, string>)[file.kind] ?? '文件'
}

function fileStatusLabel(file: SessionFileRecord): string {
  if (file.parse_status === 'ready') return '已解析'
  if (file.parse_status === 'metadata_only') return '元数据'
  if (file.parse_status === 'unsupported') return '仅保存'
  if (file.parse_status === 'error') return '解析失败'
  return '解析中'
}

function submit() {
  if (!input.value.trim() || composerUnavailable.value || conversationLocked.value || sessionFiles.uploading) return
  chat.sendMessage(input.value)
  input.value = ''
  void nextTick(resizeComposerInput)
}

async function confirmDeploymentPlan() {
  try {
    await deployments.confirmPlan()
    deployments.startPolling(chat.sessionId)
  } catch (error) {
    notifications.error(errorMessage(error))
  }
}

async function cancelDeployment() {
  try {
    await deployments.cancel()
    deployments.startPolling(chat.sessionId)
  } catch (error) {
    notifications.error(errorMessage(error))
  }
}

async function confirmDeploymentRollback() {
  try {
    await deployments.confirmRollback()
    deployments.startPolling(chat.sessionId)
  } catch (error) {
    notifications.error(errorMessage(error))
  }
}

function resizeComposerInput() {
  const element = composerInput.value
  if (!element) return
  element.style.height = 'auto'
  element.style.height = `${Math.min(Math.max(element.scrollHeight, 52), 180)}px`
}

function updateComposerClearance() {
  composerClearance.value = (composerWrap.value?.offsetHeight ?? 152) + 18
}

function handleInputKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    submit()
  }
}

watch(() => chat.entries.length, async () => {
  if (!loadingSession.value && !restoringSessionScroll.value && stickToBottom.value) {
    await scrollTimelineToBottom()
  } else {
    await nextTick()
    scheduleTurnGeometry()
  }
})

watch([() => turns.value.length, filePanelOpen], async () => {
  await nextTick()
  scheduleTurnGeometry()
})

watch([fullAccess, () => chat.busy, () => deployments.run?.status, () => sessionFiles.items.length], async () => {
  await nextTick()
  updateComposerClearance()
})

watch([() => chat.sessionId, fileUploadUnavailable], async () => {
  resetComposerDragState()
  await nextTick()
  resizeComposerInput()
})

watch(() => chat.sessionId, (sessionId, previousSessionId) => {
  if (previousSessionId && sessionId !== previousSessionId) closeFilePreview()
})

watch(() => deployments.run?.updated_at, async () => {
  if (stickToBottom.value) await scrollTimelineToBottom()
  else scheduleTurnGeometry()
})

onMounted(async () => {
  chat.connect()
  await loadSessions()
  globalThis.addEventListener('resize', scheduleTurnGeometry)
  globalThis.addEventListener('dragend', resetComposerDragState)
  globalThis.addEventListener('drop', resetComposerDragState)
  await nextTick()
  if (composerWrap.value && 'ResizeObserver' in globalThis) {
    composerResizeObserver = new ResizeObserver(updateComposerClearance)
    composerResizeObserver.observe(composerWrap.value)
  }
  updateComposerClearance()
  scheduleTurnGeometry()
})

onBeforeUnmount(() => {
  globalThis.removeEventListener('resize', scheduleTurnGeometry)
  globalThis.removeEventListener('dragend', resetComposerDragState)
  globalThis.removeEventListener('drop', resetComposerDragState)
  if (geometryFrame) cancelAnimationFrame(geometryFrame)
  composerResizeObserver?.disconnect()
  stopTransferPolling()
  deployments.stopPolling()
  resetComposerDragState()
})
</script>

<template>
  <section class="workspace-layout" :class="{ 'files-open': filePanelOpen }">
    <aside class="session-sidebar">
      <div class="sidebar-header">
        <div class="sidebar-title"><span>聊天会话</span><button class="btn btn-small btn-primary" type="button" @click="createSession">＋</button></div>
        <div class="session-search"><input v-model="search" class="input" placeholder="搜索会话" @keyup.enter="loadSessions" /><button class="btn btn-small" type="button" @click="loadSessions">搜索</button></div>
      </div>
      <div class="session-list">
        <div v-if="sessions.loading" class="sidebar-state">正在加载…</div>
        <SessionListItem
          v-for="session in sessions.items"
          v-else
          :key="session.id"
          :session="session"
          :active="session.id === chat.sessionId"
          @select="selectSession(session.id)"
          @rename="(title) => renameSession(session.id, title)"
          @pin="(pinned) => pinSession(session.id, pinned)"
          @remove="removeSession(session.id)"
        />
        <div v-if="!sessions.loading && !sessions.items.length" class="sidebar-state">没有匹配会话</div>
      </div>
    </aside>

    <div class="chat-workspace">
      <div class="workspace-toolbar">
        <div class="workspace-title">
          <strong>{{ sessions.selected?.title || '聊天工作台' }}</strong>
          <span class="connection-state" :class="{ connected: chat.connectionState === 'open' }"><span class="status-dot" :class="{ online: chat.connectionState === 'open' }" />{{ connectionLabel }}</span>
        </div>
        <div class="toolbar-controls">
          <button class="btn btn-small file-panel-toggle" type="button" @click="filePanelOpen = !filePanelOpen; filePanelCollapsed = false">附件 {{ sessionFiles.items.length }}</button>
        </div>
      </div>

      <div class="chat-timeline-shell">
        <div ref="timeline" class="chat-timeline" :style="{ paddingBottom: `${composerClearance}px` }" @scroll.passive="handleChatTimelineScroll">
          <div v-if="loadingSession" class="loading-state"><span class="spinner" /><span>正在恢复会话…</span></div>
          <div v-else-if="!chat.entries.length && !deployments.run" class="chat-welcome">
            <span class="welcome-mark">✦</span>
            <h1>今天想检查什么？</h1>
            <p>描述运维目标，Agent 会先生成命令或操作方案，并遵循当前确认模式。</p>
            <div class="prompt-suggestions">
              <button type="button" @click="input = '查看目标服务器的 CPU、内存和磁盘使用情况'">资源概览</button>
              <button type="button" @click="input = '帮我检查最近的应用错误日志'">排查错误日志</button>
              <button type="button" @click="input = '列出当前运行的 Java 服务'">查看 Java 服务</button>
            </div>
          </div>
          <template v-else>
            <template v-for="timelineItem in timelineItems" :key="timelineItem.id">
              <article
                v-if="timelineItem.kind === 'turn'"
                :ref="(element) => setTurnNode(timelineItem.turn.id, element)"
                class="chat-turn"
                :data-turn-id="timelineItem.turn.id"
              >
                <ChatTimelineItem v-if="timelineItem.turn.user" :event="timelineItem.turn.user.event" />
                <div v-if="timelineItem.turn.items.length" class="agent-turn-flow">
                  <template v-for="item in timelineItem.turn.items" :key="item.id">
                <ExecutionStep
                  v-if="item.kind === 'execution'"
                  :preview="item.preview"
                  :result="item.result"
                  :status="item.status"
                  :task-step="item.taskStep"
                  :actionable="isExecutionActionable(item.preview)"
                  :submitting="Boolean(item.preview && chat.confirmingTaskId === item.preview.task_id)"
                  @confirm="(confirmed, value) => chat.confirm(confirmed, value)"
                />
                <ChatTimelineItem
                  v-else
                  :event="item.entry.event"
                  :actionable="isPlanActionable(item.entry.event) || isFileTransferActionable(item.entry.event)"
                  :submitting="isFileTransferSubmitting(item.entry.event)"
                  :transfer-status="item.entry.event.type === 'file_transfer_preview' ? fileTransferDisplayStatus(item.entry.event, timelineItem.turn) : undefined"
                  :plan-status="item.entry.event.type === 'operation_plan' ? operationPlanStatus(item.entry.event, timelineItem.turn) : undefined"
                  @confirm="(confirmed, value) => chat.confirm(confirmed, value)"
                  @plan-confirm="(confirmed) => chat.confirmPlan(confirmed)"
                  @plan-adjust="(instruction) => chat.adjustPlan(instruction)"
                  @file-transfer-confirm="(confirmed) => chat.confirmFileTransfer(confirmed)"
                />
                  </template>
                </div>
              </article>
              <DeploymentRunCard
                v-else
                :run="timelineItem.run"
                :pending-action="deployments.pendingAction"
                :error="deployments.error"
                @confirm="confirmDeploymentPlan"
                @cancel="cancelDeployment"
                @rollback-confirm="confirmDeploymentRollback"
              />
            </template>
          </template>
        </div>

        <nav
          v-if="turns.length"
          ref="timelineRail"
          class="chat-turn-timeline"
          aria-label="会话回合时间轴"
          title="悬停查看摘要，点击跳转到对应回合"
          @mousemove="handleTimelineMove"
          @mouseleave="hoveredTurnId = ''"
          @click="handleTimelineClick"
          @wheel.prevent="handleTimelineWheel"
        >
          <span class="chat-turn-timeline-line" aria-hidden="true" />
          <span
            class="chat-turn-timeline-viewport"
            aria-hidden="true"
            :style="{ top: `${viewportIndicator.top}px`, height: `${viewportIndicator.height}px` }"
          />
          <button
            v-for="(turn, index) in turns"
            :key="turn.id"
            class="chat-turn-timeline-marker"
            :class="[turn.summary.status, { active: hoveredTurnId === turn.id }]"
            type="button"
            :aria-label="`第 ${index + 1} 回合：${turn.summary.title}`"
            :style="{ top: `${turnPositions[turn.id] ?? 18}px` }"
            @mouseenter="showTurnSummary(turn, turnPositions[turn.id] ?? 18)"
            @click.stop="jumpToTurn(turn)"
          />
          <div
            v-if="hoveredTurn"
            class="chat-turn-summary-card"
            role="status"
            :style="{ top: `${summaryCardTop}px` }"
          >
            <div class="chat-turn-summary-eyebrow">第 {{ turns.findIndex((turn) => turn.id === hoveredTurn?.id) + 1 }} 回合</div>
            <div class="chat-turn-summary-title">{{ hoveredTurn.summary.title }}</div>
            <div class="chat-turn-summary-detail">{{ hoveredTurn.summary.detail }}</div>
            <div class="chat-turn-summary-meta">
              <span class="summary-chip" :class="hoveredTurn.summary.status">{{ hoveredTurn.summary.statusLabel }}</span>
              <span v-for="chip in hoveredTurn.summary.chips" :key="chip" class="summary-chip">{{ chip }}</span>
            </div>
          </div>
        </nav>
      </div>

      <div ref="composerWrap" class="composer-wrap" :class="{ 'full-access': fullAccess }">
        <div v-if="chat.busy" class="active-task" :class="`task-${activeTaskStatus}`">
          <span class="active-task-dot" aria-hidden="true" />
          <span>{{ activeTaskLabel }}</span>
          <button class="btn btn-danger btn-small" type="button" :disabled="taskStopping" @click="chat.cancel">{{ taskStopping ? '停止中' : '停止' }}</button>
        </div>
        <div v-else-if="deployments.locksComposer" class="active-task task-deployment" role="status">
          <span class="active-task-dot" aria-hidden="true" />
          <span>部署 Runbook 正在占用当前会话，请在部署卡片中处理</span>
        </div>
        <div v-if="fullAccess" class="full-access-warning" role="alert" aria-live="polite">
          <span aria-hidden="true">!</span>
          <strong>完全访问：Opsane 当前拥有最大权限</strong>
        </div>
        <form
          class="composer"
          data-onboarding="composer"
          :class="{ 'drop-active': composerDropActive }"
          :aria-busy="loadingSession || conversationLocked || undefined"
          @submit.prevent="submit"
          @dragenter="handleComposerDragEnter"
          @dragover="handleComposerDragOver"
          @dragleave="handleComposerDragLeave"
          @drop="handleComposerDrop"
        >
          <input ref="fileInput" class="file-input" type="file" multiple :disabled="fileUploadUnavailable" @change="uploadFiles" />
          <div v-if="composerDropActive" class="composer-drop-overlay" role="status">释放文件以上传到当前会话</div>
          <textarea
            ref="composerInput"
            v-model="input"
            class="composer-input"
            rows="2"
            :placeholder="composerPlaceholder"
            :disabled="composerUnavailable || conversationLocked"
            @input="resizeComposerInput"
            @keydown="handleInputKeydown"
            @paste="handleComposerPaste"
          />
          <div class="composer-toolbar">
            <div class="composer-tools">
              <button
                class="composer-attach"
                data-onboarding="session-files"
                type="button"
                :disabled="fileUploadUnavailable"
                :aria-label="sessionFiles.uploading ? '正在上传文件' : '上传文件'"
                title="上传当前会话资料"
                @click="fileInput?.click()"
              >{{ sessionFiles.uploading ? '…' : '＋' }}</button>
              <span v-if="sessionFiles.items.length" class="composer-file-count">{{ sessionFiles.items.length }} 个文件</span>
            </div>
            <div class="composer-actions">
              <select v-model="selectedConfirmMode" class="composer-mode" data-onboarding="confirm-mode" aria-label="权限模式" title="权限模式" :disabled="composerUnavailable || conversationLocked">
                <option value="interactive">交互确认</option>
                <option value="auto_safe">安全自动</option>
                <option value="dry_run">仅预览</option>
                <option value="full_access">完全访问</option>
              </select>
              <button class="composer-send" type="submit" :disabled="!input.trim() || composerUnavailable || conversationLocked || sessionFiles.uploading" aria-label="发送">↑</button>
            </div>
          </div>
        </form>
        <div class="composer-hint">Enter 发送 · Shift+Enter 换行 · 可粘贴或拖入文件 · 命令执行仍受安全策略约束</div>
      </div>
    </div>

    <aside class="file-sidebar" :class="{ collapsed: filePanelCollapsed }">
      <div class="file-sidebar-header">
        <div class="file-summary-head">
          <span class="file-summary-icon" aria-hidden="true">▤</span>
          <span class="file-summary-copy">
            <strong>{{ sessionFiles.items.length ? `本会话 ${sessionFiles.items.length} 个文件` : '会话文件' }}</strong>
            <small>{{ filePanelSummary }}</small>
          </span>
        </div>
        <span class="file-sidebar-actions">
          <button class="file-summary-action" type="button" :disabled="fileUploadUnavailable" aria-label="上传会话文件" title="上传文件" @click="fileInput?.click()">＋</button>
          <button class="file-summary-action" type="button" :aria-label="filePanelCollapsed ? '展开会话文件' : '折叠会话文件'" :title="filePanelCollapsed ? '展开' : '折叠'" @click="filePanelCollapsed = !filePanelCollapsed">{{ filePanelCollapsed ? '⌄' : '⌃' }}</button>
          <button class="file-summary-action" type="button" aria-label="关闭会话文件" title="关闭" @click="filePanelOpen = false">×</button>
        </span>
      </div>
      <div v-if="!filePanelCollapsed" class="file-panel-body">
        <div class="file-scope-summary"><span aria-hidden="true" />仅当前会话 <small>可手动传到服务器</small></div>
        <div v-if="sessionFiles.error" class="file-error">{{ sessionFiles.error }}</div>
        <div class="file-list">
          <div v-if="sessionFiles.loading" class="file-empty">正在加载…</div>
          <div v-else-if="sessionFiles.uploading" class="file-empty"><span class="spinner" />正在上传并解析…</div>
          <div v-else-if="!sessionFiles.items.length" class="file-empty">还没有上传文件<br /><small>支持文档、日志、配置、压缩包和安装包</small></div>
          <article v-for="file in sessionFiles.items" v-else :key="file.id" class="file-row">
            <button class="file-main" type="button" @click="openFilePreview(file)">
              <span class="file-kind">{{ fileKindLabel(file) }}</span>
              <span class="file-copy">
                <strong :title="file.name">{{ file.name }}</strong>
                <small class="file-meta">
                  <time :datetime="file.created_at" :title="`上传时间：${formatCompactDateTime(file.created_at)}`">{{ formatCompactDateTime(file.created_at) }}</time>
                  <span>· {{ formatBytes(file.size) }} · {{ fileStatusLabel(file) }}</span>
                </small>
              </span>
            </button>
            <div class="file-actions">
              <button type="button" @click="openFilePreview(file)">预览</button>
              <button
                v-if="canReanalyzeLegacyFile(file)"
                type="button"
                :disabled="sessionFiles.reanalyzing[file.id]"
                @click="reanalyzeFile(file)"
              >{{ sessionFiles.reanalyzing[file.id] ? '识别中' : '重新识别' }}</button>
              <a :href="file.download_url">下载</a>
              <button
                class="file-transfer"
                type="button"
                :class="{ active: ['submitting', 'waiting_confirm', 'running'].includes(sessionFiles.transferStateForFile(file.id)?.status ?? '') }"
                :disabled="sessionFiles.transferStateForFile(file.id)?.status === 'waiting_confirm'"
                @click="openFileTransfer(file)"
              >{{ fileTransferActionLabel(file) }}</button>
              <button
                v-if="isDeployableArtifact(file)"
                class="file-deploy"
                type="button"
                :disabled="deployments.pendingAction === 'create' || deployments.locksComposer"
                @click="startFileDeployment(file)"
              >{{ deployments.pendingAction === 'create' ? '准备中' : '部署' }}</button>
              <button class="file-delete" type="button" @click="removeFile(file)">删除</button>
            </div>
            <small v-if="file.parse_error" class="file-warning" :title="file.parse_error">{{ file.parse_error }}</small>
          </article>
        </div>
      </div>
    </aside>

    <Teleport to="body">
      <div v-if="previewFile" class="file-preview-backdrop" @click.self="closeFilePreview">
        <section class="file-preview-dialog" role="dialog" aria-modal="true" :aria-label="`预览 ${previewFile.name}`">
          <header>
            <div><strong>{{ previewFile.name }}</strong><small>{{ formatBytes(previewFile.size) }} · {{ fileKindLabel(previewFile) }}</small></div>
            <div class="file-preview-actions">
              <span v-if="previewText && (isOfficePreviewFile(previewFile) || previewFile.preview_type === 'pdf')" class="file-preview-mode-switch" role="group" aria-label="预览方式">
                <button class="btn btn-small" :class="{ active: previewMode === 'formatted' }" type="button" :aria-pressed="previewMode === 'formatted'" @click="previewMode = 'formatted'">版式</button>
                <button class="btn btn-small" :class="{ active: previewMode === 'text' }" type="button" :aria-pressed="previewMode === 'text'" @click="previewMode = 'text'">提取文本</button>
              </span>
              <a class="btn btn-small" :href="previewFile.download_url">下载</a>
              <button class="btn btn-small" type="button" @click="closeFilePreview">关闭</button>
            </div>
          </header>
          <div class="file-preview-body">
            <div v-if="previewLoading && previewMode === 'formatted'" class="file-preview-state"><span class="spinner" />{{ isOfficePreviewFile(previewFile) ? '正在生成版式预览…' : '正在加载预览…' }}</div>
            <pre v-else-if="previewMode === 'text' && previewText" class="file-text-preview">{{ previewText }}</pre>
            <pre v-else-if="previewFile.preview_type === 'text'" class="file-text-preview">{{ previewText }}</pre>
            <div v-else-if="previewFile.preview_type === 'image'" class="file-image-preview" :class="{ 'has-analysis': previewText }">
              <div class="file-image-canvas"><img :src="previewFile.preview_url" :alt="previewFile.name" /></div>
              <section v-if="previewText" class="file-image-analysis">
                <strong>识别内容</strong>
                <pre>{{ previewText }}</pre>
              </section>
            </div>
            <iframe v-else-if="previewFile.preview_type === 'pdf'" :src="previewFile.preview_url" sandbox="allow-same-origin" title="保留原始版式的 PDF 预览" />
            <div v-else class="file-preview-state">{{ previewError || '该格式不支持内容预览，请下载原文件查看。' }}</div>
          </div>
        </section>
      </div>
    </Teleport>

    <SessionFileTransferDialog
      v-if="transferDialogFile"
      :file="transferDialogFile"
      :servers="inventory.servers"
      :servers-loading="inventory.loading"
      :servers-error="inventory.error"
      :state="sessionFiles.transferStateForFile(transferDialogFile.id)"
      @close="closeFileTransfer"
      @submit="transferFile"
    />
  </section>
</template>

<style scoped>
.workspace-layout { position: relative; min-height: 0; height: 100%; display: grid; grid-template-columns: 248px minmax(0, 1fr); overflow: hidden; }
.workspace-layout.files-open { grid-template-columns: 248px minmax(0, 1fr); }
.session-sidebar { min-width: 0; display: flex; flex-direction: column; border-right: 1px solid var(--border); background: var(--bg-secondary); }
.sidebar-header { display: grid; gap: 9px; padding: 10px; border-bottom: 1px solid var(--border); }
.sidebar-title { display: flex; align-items: center; justify-content: space-between; color: var(--text-secondary); font-size: 12px; font-weight: 600; }
.session-search { display: flex; align-items: center; gap: 5px; }
.session-search .input { min-width: 0; min-height: 28px; height: 28px; flex: 1; padding: 4px 8px; font-size: 11px; }
.session-search .btn { min-height: 28px; height: 28px; flex: 0 0 auto; padding: 4px 8px; line-height: 1; white-space: nowrap; }
.session-list { flex: 1; min-height: 0; overflow: auto; padding: 7px; }
.sidebar-state { padding: 24px 8px; color: var(--text-muted); text-align: center; font-size: 12px; }
.chat-workspace { position: relative; min-width: 0; min-height: 0; height: 100%; display: grid; grid-template-rows: 47px minmax(0, 1fr); overflow: hidden; background: linear-gradient(180deg, rgba(23,104,255,.035), transparent 180px), var(--bg-primary); }
.workspace-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 0 15px; border-bottom: 1px solid var(--border); background: var(--bg-secondary); }
.workspace-title { display: flex; align-items: center; gap: 10px; min-width: 0; }
.workspace-title > strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.connection-state { display: inline-flex; align-items: center; gap: 6px; color: var(--text-muted); font-size: 10px; }
.connection-state.connected { color: var(--success); }
.toolbar-controls { display: flex; gap: 7px; }
.file-panel-toggle { white-space: nowrap; }
.chat-timeline-shell { position: relative; min-width: 0; min-height: 0; overflow: hidden; }
.chat-timeline { width: 100%; height: 100%; min-width: 0; min-height: 0; display: flex; flex-direction: column; gap: 18px; overflow-x: hidden; overflow-y: auto; padding: 22px max(18px, calc((100% - 980px) / 2)) 22px max(74px, calc((100% - 980px) / 2)); box-sizing: border-box; scroll-behavior: auto; }
.chat-timeline > * { flex-shrink: 0; }
.chat-turn { width: 100%; min-width: 0; display: flex; flex-direction: column; gap: 15px; }
.agent-turn-flow { width: min(920px,100%); min-width: 0; align-self: center; display: flex; flex-direction: column; gap: 14px; overflow-x: hidden; }
.chat-turn-timeline { position: absolute; z-index: 30; top: 0; bottom: 0; left: 0; width: 56px; cursor: pointer; }
.chat-turn-timeline-line { position: absolute; top: 18px; bottom: 18px; left: 25px; border-left: 2px dashed rgba(72,132,168,.28); pointer-events: none; }
.chat-turn-timeline-viewport { position: absolute; left: 23px; width: 5px; min-height: 16px; border-radius: 3px; background: rgba(24,184,231,.34); pointer-events: none; }
.chat-turn-timeline-marker { --marker-color: rgba(113,147,177,.58); position: absolute; left: 0; width: 56px; height: 24px; padding: 0; border: 0; background: transparent; transform: translateY(-50%); cursor: pointer; }
.chat-turn-timeline-marker::before { content: ""; position: absolute; top: 50%; left: 18px; width: 15px; height: 2px; border-radius: 1px; background: var(--marker-color); transform: translateY(-50%); transition: left .12s ease,width .12s ease,background .12s ease; }
.chat-turn-timeline-marker.success { --marker-color: rgba(54,217,149,.76); }
.chat-turn-timeline-marker.warning { --marker-color: rgba(241,187,97,.78); }
.chat-turn-timeline-marker.danger { --marker-color: rgba(255,112,111,.8); }
.chat-turn-timeline-marker.active::before,.chat-turn-timeline-marker:focus-visible::before { left: 12px; width: 27px; height: 3px; background: var(--brand-cyan); box-shadow: 0 0 10px rgba(0,188,232,.38); }
.chat-turn-timeline-marker:focus { outline: 0; }
.chat-turn-summary-card { position: absolute; z-index: 2; left: 52px; width: min(640px,calc(100vw - 88px)); min-width: 0; padding: 16px 18px 14px; border: 1px solid rgba(24,184,231,.24); border-radius: 14px; background: var(--summary-surface); box-shadow: var(--shadow); pointer-events: none; backdrop-filter: blur(16px); }
.chat-turn-summary-eyebrow { margin-bottom: 6px; color: var(--text-muted); font-size: 11px; }
.chat-turn-summary-title { overflow: hidden; color: var(--text-primary); font-size: 15px; font-weight: 680; line-height: 1.42; text-overflow: ellipsis; white-space: nowrap; }
.chat-turn-summary-detail { display: -webkit-box; overflow: hidden; margin-top: 8px; color: var(--text-muted); font-size: 13px; line-height: 1.55; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.chat-turn-summary-meta { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.summary-chip { display: inline-flex; align-items: center; max-width: 220px; min-height: 22px; padding: 2px 8px; overflow: hidden; border: 1px solid var(--border); border-radius: 999px; background: var(--surface-soft); color: var(--text-muted); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.summary-chip.success { color: var(--success-text); border-color: rgba(54,217,149,.3); background: var(--success-soft); }
.summary-chip.warning { color: var(--warning-text); border-color: rgba(241,187,97,.34); background: var(--warning-soft); }
.summary-chip.danger { color: var(--danger-text); border-color: rgba(255,112,111,.34); background: var(--danger-soft); }
.chat-welcome { margin: auto; max-width: 640px; display: grid; justify-items: center; padding: 32px; text-align: center; }
.welcome-mark { width: 44px; height: 44px; display: grid; place-items: center; border-radius: 13px; background: var(--brand-gradient); color: #fff; font-size: 20px; box-shadow: var(--brand-shadow); }
.chat-welcome h1 { margin: 16px 0 7px; font-size: 24px; }
.chat-welcome p { margin: 0; color: var(--text-secondary); line-height: 1.55; }
.prompt-suggestions { display: flex; flex-wrap: wrap; justify-content: center; gap: 8px; margin-top: 20px; }
.prompt-suggestions button { padding: 8px 11px; border: 1px solid var(--border); border-radius: 999px; background: var(--bg-secondary); color: var(--text-secondary); }
.prompt-suggestions button:hover { border-color: var(--accent); color: var(--text-primary); }
.composer-wrap { position: absolute; z-index: 35; right: 0; bottom: 0; left: 0; padding: 34px max(16px, calc((100% - 900px) / 2)) 12px; background: linear-gradient(180deg, transparent, var(--composer-fade) 38%, var(--bg-primary) 72%); pointer-events: none; }
.active-task { display: flex; align-items: center; gap: 8px; margin-bottom: 7px; color: var(--text-secondary); font-size: 11px; }
.active-task .btn { margin-left: auto; }
.active-task-dot { width: 7px; height: 7px; flex: 0 0 auto; border-radius: 50%; background: var(--warning); box-shadow: 0 0 0 3px var(--warning-soft); animation: task-pulse 1.4s ease-in-out infinite; }
.active-task.task-waiting_confirm .active-task-dot,.active-task.task-planning .active-task-dot { animation: none; }
.active-task.task-stopping .active-task-dot { background: var(--danger); box-shadow: 0 0 0 3px var(--danger-soft); }
@keyframes task-pulse { 50% { opacity: .42; } }
.active-task,.full-access-warning,.composer,.composer-hint { pointer-events: auto; }
.composer { position: relative; display: grid; gap: 8px; padding: 12px; border: 1px solid rgba(39,103,139,.46); border-radius: 19px; background: var(--glass-surface); box-shadow: var(--floating-shadow); backdrop-filter: blur(18px); }
.file-input { display: none; }
.composer:focus-within { border-color: rgba(24,184,231,.72); box-shadow: 0 0 0 2px var(--accent-soft), var(--floating-shadow); }
.composer.drop-active { border-color: rgba(24,184,231,.88); box-shadow: 0 0 0 3px var(--accent-soft), var(--floating-shadow); }
.composer-drop-overlay { position: absolute; z-index: 5; inset: 5px; display: grid; place-items: center; border: 1px dashed rgba(24,184,231,.78); border-radius: 15px; background: var(--drop-overlay); color: var(--code-accent); font-size: 13px; font-weight: 650; letter-spacing: .01em; pointer-events: none; }
.composer-input { width: 100%; min-height: 52px; max-height: 180px; padding: 2px 3px; overflow-y: auto; border: 0; outline: 0; background: transparent; color: var(--text-primary); font-size: 14px; line-height: 1.55; resize: none; }
.composer-input::placeholder { color: var(--text-muted); }
.composer-toolbar,.composer-tools,.composer-actions { display: flex; align-items: center; }
.composer-toolbar { min-width: 0; justify-content: space-between; gap: 12px; }
.composer-tools,.composer-actions { gap: 6px; }
.composer-attach { width: 32px; height: 32px; flex: 0 0 auto; border: 1px solid transparent; border-radius: 50%; background: transparent; color: var(--text-secondary); font-size: 20px; line-height: 1; }
.composer-attach:hover:not(:disabled) { border-color: var(--border); background: var(--bg-tertiary); color: var(--text-primary); }
.composer-file-count { color: var(--text-muted); font-size: 11px; white-space: nowrap; }
.composer-mode { width: auto; max-width: 142px; min-width: 96px; height: 32px; flex: 0 0 auto; padding: 4px 25px 4px 9px; border: 1px solid transparent; border-radius: 9px; outline: 0; background: transparent; color: var(--text-secondary); font-size: 11px; }
.composer-mode:hover,.composer-mode:focus { border-color: var(--border-light); color: var(--text-primary); }
.composer-send { width: 32px; height: 32px; flex: 0 0 auto; border: 0; border-radius: 50%; background: var(--brand-gradient); color: #fff; font-size: 18px; font-weight: 750; box-shadow: 0 5px 15px rgba(0,188,232,.2); }
.composer-send:hover:not(:disabled) { filter: brightness(1.12); }
.composer-send:disabled { background: var(--bg-tertiary); color: var(--text-muted); opacity: 1; }
.composer-hint { margin-top: 5px; color: var(--text-muted); font-size: 10px; text-align: center; }
.full-access-warning { display: flex; align-items: center; gap: 8px; margin-bottom: 7px; padding: 6px 9px; border: 1px solid rgba(224,81,73,.34); border-radius: 7px; background: var(--danger-soft); color: var(--danger-text); font-size: 12px; line-height: 1.35; }
.full-access-warning > span { width: 17px; height: 17px; flex: 0 0 17px; display: grid; place-items: center; border-radius: 50%; background: #e05149; color: #fff; font-size: 11px; font-weight: 800; }
.composer-wrap.full-access .composer { border-color: rgba(224,81,73,.9); background: var(--full-access-surface); box-shadow: 0 0 0 1px rgba(224,81,73,.28),var(--floating-shadow); }
.composer-wrap.full-access .composer:focus-within { border-color: #e05149; box-shadow: 0 0 0 2px rgba(224,81,73,.24),var(--floating-shadow); }
.composer-wrap.full-access .composer-input { caret-color: var(--full-access-caret); }
.composer-wrap.full-access .composer-mode { border-color: rgba(224,81,73,.72); background: var(--full-access-control); color: var(--full-access-text); }
.composer-wrap.full-access .composer-send { background: #e05149; }
.file-sidebar { position: absolute; z-index: 45; top: 58px; right: 12px; width: 328px; min-width: 0; min-height: 0; max-height: min(520px,calc(100% - 76px)); display: none; flex-direction: column; overflow: hidden; border: 1px solid rgba(24,184,231,.24); border-radius: 8px; background: var(--glass-surface); box-shadow: var(--floating-shadow); backdrop-filter: blur(18px); }
.files-open .file-sidebar { display: flex; }
.file-sidebar.collapsed { max-height: none; }
.file-sidebar-header { min-height: 72px; display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 11px 10px 11px 12px; }
.file-summary-head { min-width: 0; display: flex; align-items: center; gap: 10px; }
.file-summary-icon { width: 38px; height: 38px; flex: 0 0 auto; display: grid; place-items: center; border-radius: 8px; background: var(--accent-soft); color: var(--accent); font-size: 18px; box-shadow: inset 0 0 0 1px rgba(24,184,231,.12); }
.file-summary-copy { min-width: 0; display: grid; gap: 4px; }
.file-summary-copy strong { overflow: hidden; color: var(--text-primary); font-size: 13px; font-weight: 620; text-overflow: ellipsis; white-space: nowrap; }
.file-summary-copy small { color: var(--text-muted); font-size: 10px; }
.file-sidebar-actions { display: flex; align-items: center; gap: 1px; }
.file-summary-action { width: 26px; height: 26px; display: grid; place-items: center; padding: 0; border: 0; border-radius: 6px; background: transparent; color: var(--text-muted); font-size: 15px; line-height: 1; }
.file-summary-action:hover,.file-summary-action:focus-visible { outline: 0; background: var(--surface-hover); color: var(--text-primary); }
.file-panel-body { min-height: 0; display: flex; flex-direction: column; border-top: 1px solid var(--border); }
.file-scope-summary { min-height: 34px; display: flex; align-items: center; gap: 6px; padding: 7px 12px; border-bottom: 1px solid var(--border); color: var(--text-secondary); font-size: 10px; }
.file-scope-summary > span { width: 6px; height: 6px; flex: 0 0 auto; border-radius: 50%; background: var(--success); }
.file-scope-summary small { margin-left: auto; color: var(--text-muted); font-size: 10px; }
.file-error { margin: 8px; padding: 8px; border: 1px solid rgba(248,81,73,.35); border-radius: 6px; background: var(--danger-soft); color: var(--danger-text); font-size: 10px; }
.file-list { min-height: 0; max-height: 390px; overflow: auto; padding: 0 11px 6px; }
.file-empty { min-height: 126px; display: grid; place-content: center; justify-items: center; gap: 8px; padding: 16px; color: var(--text-muted); text-align: center; font-size: 11px; line-height: 1.55; }
.file-empty .spinner { width: 18px; height: 18px; }
.file-row { padding: 10px 1px 9px; border-bottom: 1px solid var(--border); }
.file-row:last-child { border-bottom: 0; }
.file-main { width: 100%; min-width: 0; display: flex; align-items: center; gap: 8px; padding: 0; border: 0; background: transparent; color: inherit; text-align: left; }
.file-kind { min-width: 38px; padding: 4px 5px; border-radius: 5px; background: var(--surface-hover); color: var(--text-secondary); font-size: 9px; text-align: center; }
.file-copy { min-width: 0; flex: 1; display: grid; gap: 3px; }
.file-copy strong { overflow: hidden; color: var(--text-primary); font-size: 11px; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.file-copy small { color: var(--text-muted); font-size: 9px; }
.file-meta { min-width: 0; display: flex; align-items: center; gap: 4px; overflow: hidden; white-space: nowrap; }
.file-meta time { flex: none; }
.file-meta span { overflow: hidden; text-overflow: ellipsis; }
.file-actions { display: flex; align-items: center; gap: 9px; margin: 7px 0 0 46px; opacity: .62; transition: opacity .12s ease; }
.file-row:hover .file-actions,.file-row:focus-within .file-actions { opacity: 1; }
.file-actions button,.file-actions a { padding: 0; border: 0; background: transparent; color: var(--text-secondary); font-size: 9px; text-decoration: none; }
.file-actions button:hover,.file-actions a:hover { color: var(--accent); }
.file-actions button:disabled { cursor: not-allowed; opacity: .45; }
.file-actions .file-transfer.active { color: var(--accent); }
.file-actions .file-deploy { color: var(--success); }
.file-actions .file-delete:hover { color: var(--danger); }
.file-warning { display: block; overflow: hidden; margin: 6px 0 0 46px; color: var(--warning); font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
.file-preview-backdrop { position: fixed; z-index: 1000; inset: 0; display: grid; place-items: center; padding: 30px; background: rgba(0,0,0,.68); }
.file-preview-dialog { width: min(1050px, 94vw); height: min(780px, 90vh); display: grid; grid-template-rows: auto minmax(0,1fr); overflow: hidden; border: 1px solid var(--border-light); border-radius: 12px; background: var(--bg-secondary); box-shadow: 0 24px 80px rgba(0,0,0,.5); }
.file-preview-dialog > header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 12px; border-bottom: 1px solid var(--border); }
.file-preview-dialog > header > div:first-child { min-width: 0; display: grid; gap: 3px; }
.file-preview-dialog > header strong { overflow: hidden; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.file-preview-dialog > header small { color: var(--text-muted); font-size: 10px; }
.file-preview-actions { display: flex; gap: 7px; }
.file-preview-actions a { text-decoration: none; }
.file-preview-mode-switch { display: flex; align-items: center; gap: 2px; margin-right: 3px; padding: 2px; border: 1px solid var(--border); border-radius: 7px; background: var(--surface-hover); }
.file-preview-mode-switch .btn { border-color: transparent; background: transparent; color: var(--text-muted); }
.file-preview-mode-switch .btn.active { border-color: var(--border-light); background: var(--bg-secondary); color: var(--text-primary); box-shadow: 0 1px 4px rgba(0,0,0,.18); }
.file-preview-body { min-height: 0; overflow: auto; background: var(--terminal-bg); }
.file-preview-body img { width: 100%; height: 100%; object-fit: contain; }
.file-preview-body iframe { width: 100%; height: 100%; border: 0; background: #fff; }
.file-image-preview { height: 100%; min-height: 0; }
.file-image-preview.has-analysis { display: grid; grid-template-columns: minmax(0,1.45fr) minmax(280px,.75fr); }
.file-image-canvas { min-width: 0; min-height: 0; padding: 12px; overflow: auto; }
.file-image-analysis { min-width: 0; padding: 14px; overflow: auto; border-left: 1px solid var(--border); background: var(--bg-secondary); }
.file-image-analysis > strong { display: block; margin-bottom: 9px; color: var(--text-primary); font-size: 11px; }
.file-image-analysis pre { margin: 0; color: var(--terminal-text); font: 10px/1.58 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; white-space: pre-wrap; word-break: break-word; }
.file-text-preview { min-height: 100%; margin: 0; padding: 18px; color: var(--terminal-text); font: 11px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; white-space: pre-wrap; word-break: break-word; }
.file-preview-state { height: 100%; display: grid; place-content: center; justify-items: center; gap: 10px; color: var(--text-muted); font-size: 12px; }
@media (max-width: 1100px) { .workspace-layout.files-open { grid-template-columns: 220px minmax(0,1fr); } .files-open .file-sidebar { right: 10px; } }
@media (max-width: 780px) { .workspace-layout,.workspace-layout.files-open { grid-template-columns: 1fr; } .session-sidebar { display: none; } .files-open .file-sidebar { width: min(320px,calc(100vw - 20px)); } .file-preview-backdrop { padding: 10px; } .file-image-preview.has-analysis { grid-template-columns: 1fr; grid-template-rows: minmax(260px,1fr) auto; } .file-image-analysis { max-height: 38vh; border-top: 1px solid var(--border); border-left: 0; } .chat-turn-timeline { width: 42px; } .chat-turn-timeline-line { left: 18px; } .chat-turn-timeline-viewport { left: 16px; } .chat-turn-timeline-marker { width: 42px; } .chat-turn-timeline-marker::before { left: 12px; } .chat-turn-timeline-marker.active::before { left: 7px; } .chat-turn-summary-card { left: 40px; padding: 13px 14px 12px; } .chat-timeline { padding-left: 54px; } .composer-mode { max-width: 112px; min-width: 86px; } }
</style>
