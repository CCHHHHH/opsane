import { describe, expect, it } from 'vitest'

import appSource from '../App.vue?raw'
import chatTimelineItemSource from '../components/chat/ChatTimelineItem.vue?raw'
import deploymentRunCardSource from '../components/chat/DeploymentRunCard.vue?raw'
import executionStepSource from '../components/chat/ExecutionStep.vue?raw'
import onboardingTourSource from '../components/common/OnboardingTour.vue?raw'
import chatPageSource from './ChatPage.vue?raw'
import serversPageSource from './ServersPage.vue?raw'
import terminalPageSource from './TerminalPage.vue?raw'

function rule(source: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return source.match(new RegExp(`${escaped}\\s*\\{([^}]+)\\}`))?.[1] ?? ''
}

describe('full-height workbench layout contract', () => {
  it.each([
    ['chat outer grid', chatPageSource, '.workspace-layout'],
    ['chat workspace', chatPageSource, '.chat-workspace'],
    ['terminal outer grid', terminalPageSource, '.terminal-layout'],
    ['terminal workspace', terminalPageSource, '.terminal-workspace'],
  ])('%s prevents long history from expanding the application grid', (_name, source, selector) => {
    const declarations = rule(source, selector)
    expect(declarations).toMatch(/min-height\s*:\s*0/)
    expect(declarations).toMatch(/height\s*:\s*100%/)
    expect(declarations).toMatch(/overflow\s*:\s*hidden/)
  })

  it('keeps the chat session search row compact and single-line', () => {
    for (const source of [chatPageSource, terminalPageSource]) {
      const input = rule(source, '.session-search .input')
      const button = rule(source, '.session-search .btn')

      expect(input).toMatch(/height\s*:\s*28px/)
      expect(input).toMatch(/flex\s*:\s*1/)
      expect(button).toMatch(/height\s*:\s*28px/)
      expect(button).toMatch(/white-space\s*:\s*nowrap/)
    }
  })

  it('keeps a single server card at a stable readable width', () => {
    const declarations = rule(serversPageSource, '.server-grid')
    expect(declarations).toMatch(/repeat\(auto-fill,\s*minmax\(300px,\s*380px\)\)/)
    expect(declarations).toMatch(/justify-content\s*:\s*start/)
  })

  it('tests new or changed SSH connection details before saving a server', () => {
    expect(serversPageSource).toContain('await inventory.testServerConnection(input)')
    expect(serversPageSource).toContain("? '测试并保存'")
    expect(serversPageSource).toContain("if ((!originalAlias.value || connectionChanged) && !serverConnectionVerified.value)")
  })

  it('keeps timeline entries at their content height so execution results remain visible', () => {
    const declarations = rule(chatPageSource, '.chat-timeline > *')
    expect(declarations).toMatch(/flex-shrink\s*:\s*0/)
  })

  it('does not replace automatic target selection with the first inventory server', () => {
    expect(chatPageSource).not.toContain('chat.target = inventory.servers[0].alias')
    expect(chatPageSource).not.toContain('v-model="chat.target"')
    expect(chatPageSource).not.toContain('aria-label="目标服务器"')
  })

  it('places the chat permission selector immediately before the send button', () => {
    const modeIndex = chatPageSource.indexOf('class="composer-mode"')
    const sendIndex = chatPageSource.indexOf('class="composer-send"')
    expect(modeIndex).toBeGreaterThan(-1)
    expect(sendIndex).toBeGreaterThan(modeIndex)
    expect(chatPageSource).toContain('class="composer-toolbar"')
    expect(chatPageSource).toContain('class="composer-actions"')
  })

  it('left-aligns all chat timeline confirmation actions', () => {
    for (const [source, selector] of [
      [executionStepSource, '.execution-confirm-actions'],
      [chatTimelineItemSource, '.structured-actions'],
      [deploymentRunCardSource, '.deployment-actions'],
    ]) {
      expect(rule(source, selector)).toMatch(/justify-content\s*:\s*flex-start/)
    }
  })

  it('accepts session files from the picker, clipboard, and composer drop target', () => {
    expect(chatPageSource).toContain('@paste="handleComposerPaste"')
    expect(chatPageSource).toContain('@dragenter="handleComposerDragEnter"')
    expect(chatPageSource).toContain('@dragover="handleComposerDragOver"')
    expect(chatPageSource).toContain('@drop="handleComposerDrop"')
    expect(chatPageSource).toContain('void uploadSessionFiles(files)')
    expect(chatPageSource).toContain('watch([() => chat.sessionId, fileUploadUnavailable]')
    expect(chatPageSource).toContain("notifications.error('当前会话暂时无法接收文件，请等待当前任务完成')")
    expect(chatPageSource).toContain('释放文件以上传到当前会话')
    expect(chatPageSource).toContain('可粘贴或拖入文件')
  })

  it('keeps conversational file transfer confirmation in the chat timeline', () => {
    expect(chatPageSource).toContain('isFileTransferActionable(item.entry.event)')
    expect(chatPageSource).toContain(':submitting="isFileTransferSubmitting(item.entry.event)"')
    expect(chatPageSource).toContain(':transfer-status="item.entry.event.type === \'file_transfer_preview\' ? fileTransferDisplayStatus(item.entry.event, timelineItem.turn) : undefined"')
    expect(chatPageSource).toContain('@file-transfer-confirm="(confirmed) => chat.confirmFileTransfer(confirmed)"')
  })

  it('projects the deployment card chronologically instead of pinning it below every chat turn', () => {
    expect(chatPageSource).toContain('projectChatTimeline(turns.value, deployments.run)')
    expect(chatPageSource).toContain('v-for="timelineItem in timelineItems"')
    expect(chatPageSource).toContain("v-if=\"timelineItem.kind === 'turn'\"")
    expect(chatPageSource).toContain(':run="timelineItem.run"')
    expect(chatPageSource).not.toContain('v-if="deployments.run"\n            :run="deployments.run"')
  })

  it('offers registered deployment for both JAR and WAR session artifacts', () => {
    expect(chatPageSource).toContain("if (extension === '.jar' || name.endsWith('.jar')) return 'jar'")
    expect(chatPageSource).toContain("if (extension === '.war' || name.endsWith('.war')) return 'war'")
    expect(chatPageSource).toContain("String(service.artifact_type || 'jar').toLowerCase() === artifactType")
    expect(chatPageSource).toContain('v-if="isDeployableArtifact(file)"')
  })

  it('shows persisted image recognition beside the original image preview', () => {
    expect(chatPageSource).toContain("['text', 'image', 'pdf'].includes(file.preview_type)")
    expect(chatPageSource).toContain('class="file-image-preview"')
    expect(chatPageSource).toContain('class="file-image-analysis"')
    expect(chatPageSource).toContain('识别内容')
  })

  it('renders Office files with preserved layout and keeps extracted text available', () => {
    expect(chatPageSource).toContain("const officePreviewExtensions = new Set(['.doc', '.docx', '.xls', '.xlsx', '.xlsm', '.ppt', '.pptx'])")
    expect(chatPageSource).toContain('sessionFiles.renderPreview(file)')
    expect(chatPageSource).toContain("previewMode.value = 'text'")
    expect(chatPageSource).toContain('正在生成版式预览…')
    expect(chatPageSource).toContain('>版式</button>')
    expect(chatPageSource).toContain('>提取文本</button>')
    expect(chatPageSource).toContain('title="保留原始版式的 PDF 预览"')
  })

  it('floats the composer over the chat surface without covering the final message', () => {
    const composer = rule(chatPageSource, '.composer-wrap')
    expect(composer).toMatch(/position\s*:\s*absolute/)
    expect(composer).toMatch(/bottom\s*:\s*0/)
    expect(chatPageSource).toContain('composerClearance')
    expect(chatPageSource).toContain('paddingBottom: `${composerClearance}px`')
  })

  it('restores each selected chat session at its previous reading position', () => {
    expect(chatPageSource).toContain('const sessionScrollStates = new Map<string, SessionScrollState>()')
    expect(chatPageSource).toContain('captureSessionScrollState(chat.sessionId)')
    expect(chatPageSource).toMatch(/loadingSession\.value = false\s+await restoreSessionScrollPosition\(id\)/)
    expect(chatPageSource).toContain('anchorTurnId?: string')
    expect(chatPageSource).toContain('anchorNode.offsetTop - scroller.scrollTop')
    expect(chatPageSource).toContain('loadingSession.value || restoringSessionScroll.value')
    expect(chatPageSource).toContain('@scroll.passive="handleChatTimelineScroll"')
  })

  it('restores sessions immediately while keeping explicit timeline jumps smooth', () => {
    const timeline = rule(chatPageSource, '.chat-timeline')
    expect(timeline).toMatch(/scroll-behavior\s*:\s*auto/)
    expect(chatPageSource).toContain("behavior: 'smooth'")
  })

  it('keeps the composer disabled until session recovery and websocket connection are ready', () => {
    expect(chatPageSource).toContain('const composerUnavailable = computed')
    expect(chatPageSource).toContain('loadingSession.value')
    expect(chatPageSource).toContain('restoringSessionScroll.value')
    expect(chatPageSource).toContain("chat.connectionState !== 'open'")
    expect(chatPageSource).toContain('const conversationLocked = computed')
    expect(chatPageSource).toContain(':disabled="composerUnavailable || conversationLocked"')
  })

  it('renders the session file panel as a pinned Codex-style summary', () => {
    const panel = rule(chatPageSource, '.file-sidebar')
    expect(panel).toMatch(/position\s*:\s*absolute/)
    expect(panel).toMatch(/right\s*:\s*12px/)
    expect(panel).toMatch(/width\s*:\s*328px/)
    expect(panel).toMatch(/max-height\s*:\s*min\(520px,calc\(100% - 76px\)\)/)
    expect(panel).toMatch(/border-radius\s*:\s*8px/)
    expect(panel).toMatch(/box-shadow\s*:/)
    expect(panel).toMatch(/backdrop-filter\s*:\s*blur/)
    expect(chatPageSource).toContain('class="file-summary-head"')
    expect(chatPageSource).toContain('filePanelCollapsed')
    expect(chatPageSource).not.toContain("'files-expanded'")
    expect(chatPageSource).not.toMatch(/\.files-expanded\s+\.chat-timeline/)
    expect(chatPageSource).toContain('本会话 ${sessionFiles.items.length} 个文件')
  })

  it('removes misleading global server status and terminal mode selectors', () => {
    expect(appSource).not.toContain('server-chip')
    expect(terminalPageSource).not.toContain('terminal.confirmMode')
    expect(terminalPageSource).not.toContain('安全自动')
    expect(terminalPageSource).not.toContain('完全访问')
  })

  it('provides an accessible persistent light and dark theme switch', () => {
    expect(appSource).toContain('class="theme-switch"')
    expect(appSource).toContain('role="switch"')
    expect(appSource).toContain(':aria-checked="theme === \'light\'"')
    expect(appSource).toContain(':aria-label="themeSwitchLabel"')
    expect(appSource).toContain('applyTheme(nextTheme(theme.value))')
  })

  it('provides a persistent, restartable first-use tour over real workbench controls', () => {
    expect(appSource).toContain('<OnboardingTour ref="onboardingTour" />')
    expect(appSource).toContain('aria-label="打开新手引导"')
    expect(appSource).toContain("'/config': 'config'")
    expect(appSource).toContain("'/servers': 'servers'")
    expect(appSource).toContain("'/audit': 'audit'")
    expect(chatPageSource).toContain('data-onboarding="composer"')
    expect(chatPageSource).toContain('data-onboarding="confirm-mode"')
    expect(chatPageSource).toContain('data-onboarding="session-files"')
    expect(onboardingTourSource).toContain('shouldShowOnboarding()')
    expect(onboardingTourSource).toContain('completeOnboarding()')
    expect(onboardingTourSource).toContain('defineExpose({ start })')
    expect(onboardingTourSource).toContain('target: \'[data-onboarding="config"]\'')
    expect(onboardingTourSource).toContain("title: '先连接 LLM'")
    expect(onboardingTourSource).toContain('先测试连接，成功后保存')
    expect(rule(onboardingTourSource, '.onboarding-actions')).toMatch(/justify-content\s*:\s*flex-start/)
  })

  it('preserves the legacy Codex-style chat interaction contract in the Vue workbench', () => {
    expect(chatPageSource).toContain('class="chat-turn-timeline"')
    expect(chatPageSource).toContain('class="chat-turn-summary-card"')
    expect(chatPageSource).toContain('jumpToTurn(turn)')
    expect(chatPageSource).toContain('<ExecutionStep')
    expect(chatPageSource).toContain("'full-access': fullAccess")
    expect(chatPageSource).toContain('Opsane 当前拥有最大权限')
  })

  it('uses exact turn-state labels and removes the stale global footer', () => {
    expect(chatPageSource).toContain('activeTaskState')
    expect(chatPageSource).toContain('activeTaskLabel')
    expect(chatPageSource).toContain('{{ activeTaskLabel }}')
    expect(appSource).not.toContain('app-footer')
    expect(appSource).toContain('<ToastViewport />')
  })
})
