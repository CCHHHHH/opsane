import { defineStore } from 'pinia'

import { errorMessage, http } from '../api/http'

export type SessionFilePreviewType = 'text' | 'pdf' | 'image' | 'none'

export interface SessionFileRecord {
  id: string
  session_id: string
  name: string
  media_type: string
  extension: string
  kind: string
  preview_type: SessionFilePreviewType
  size: number
  sha256: string
  parse_status: string
  parse_error: string
  layout_preview_status?: string
  layout_preview_error?: string
  layout_preview_size?: number
  metadata: Record<string, unknown>
  created_at: string
  preview_url: string
  content_url: string
  download_url: string
}

const inFlightPreviewRenders = new Map<string, Promise<SessionFileRecord>>()
const PREVIEW_POLL_INTERVAL_MS = 500
const PREVIEW_POLL_ATTEMPTS = 240

function wait(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds))
}

export interface SessionFileContent {
  id: string
  name: string
  content: string
  truncated: boolean
  parse_status: string
  parse_error: string
  metadata: Record<string, unknown>
}

export interface SessionFileTransferInput {
  target: string
  remote_dir: string
  remote_name: string
  overwrite: boolean
}

export interface SessionFileTransferRecord {
  id: string
  request_id: string
  session_id: string
  file_id: string
  file_name: string
  target: string
  remote_dir: string
  remote_name: string
  remote_path: string
  size: number
  sha256: string
  remote_size?: number
  remote_sha256?: string
  overwrite?: boolean
  status: string
  error: string
  message: string
  created_at: string
  updated_at?: string
  completed_at?: string
}

export interface SessionFileTransferState {
  status: 'submitting' | 'waiting_confirm' | 'running' | 'success' | 'failed' | 'canceled'
  requestId: string
  error: string
  result: SessionFileTransferRecord | null
}

function requestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `transfer-${crypto.randomUUID()}`
  }
  return `transfer-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
}

function transferSucceeded(status: string): boolean {
  return ['success', 'succeeded', 'completed', 'complete'].includes(status.toLowerCase())
}

function transferFailed(status: string): boolean {
  return ['failed', 'error', 'timeout', 'interrupted'].includes(status.toLowerCase())
}

function stateFromTransfer(transfer: SessionFileTransferRecord): SessionFileTransferState['status'] {
  const status = String(transfer.status || '')
  if (status.toLowerCase() === 'waiting_confirm') return 'waiting_confirm'
  if (['canceled', 'cancelled'].includes(status.toLowerCase())) return 'canceled'
  if (transferSucceeded(status)) return 'success'
  if (transferFailed(status)) return 'failed'
  return 'running'
}

export const useSessionFilesStore = defineStore('sessionFiles', {
  state: () => ({
    sessionId: '',
    items: [] as SessionFileRecord[],
    loading: false,
    uploading: false,
    reanalyzing: {} as Record<string, boolean>,
    renderingPreviews: {} as Record<string, boolean>,
    error: '',
    transfers: [] as SessionFileTransferRecord[],
    transferStates: {} as Record<string, SessionFileTransferState>,
  }),
  getters: {
    transferStateForFile: (state) => (fileId: string): SessionFileTransferState | undefined => (
      state.transferStates[fileId]
    ),
  },
  actions: {
    async load(sessionId: string) {
      this.sessionId = sessionId
      this.loading = true
      this.error = ''
      try {
        const [filesData, transfersData] = await Promise.all([
          http.get<{ files: SessionFileRecord[] }>(
            `/api/sessions/${encodeURIComponent(sessionId)}/files`,
          ),
          http.get<{ transfers: SessionFileTransferRecord[] }>(
            `/api/sessions/${encodeURIComponent(sessionId)}/file-transfers`,
          ),
        ])
        if (this.sessionId === sessionId) {
          this.items = filesData.files ?? []
          this.restoreTransfers(transfersData.transfers ?? [])
        }
      } catch (error) {
        if (this.sessionId === sessionId) this.error = errorMessage(error)
      } finally {
        if (this.sessionId === sessionId) this.loading = false
      }
    },
    async refreshTransfers(sessionId: string) {
      const data = await http.get<{ transfers: SessionFileTransferRecord[] }>(
        `/api/sessions/${encodeURIComponent(sessionId)}/file-transfers`,
      )
      if (this.sessionId === sessionId) this.restoreTransfers(data.transfers ?? [])
      return data.transfers ?? []
    },
    async upload(sessionId: string, files: File[]) {
      if (!files.length) return []
      // All upload entry points share this action. Suppress a second gesture
      // while the first multipart request is still in flight.
      if (this.uploading) return []
      this.uploading = true
      this.error = ''
      try {
        const form = new FormData()
        for (const file of files) form.append('files', file)
        const data = await http.post<{ files: SessionFileRecord[] }>(
          `/api/sessions/${encodeURIComponent(sessionId)}/files`,
          form,
        )
        // The user may switch conversations while a large file is uploading.
        // Never let the late response select or repopulate the old session.
        if (this.sessionId === sessionId) await this.load(sessionId)
        return data.files ?? []
      } catch (error) {
        if (this.sessionId === sessionId) {
          this.error = errorMessage(error)
          await this.load(sessionId)
          this.error = errorMessage(error)
        }
        throw error
      } finally {
        this.uploading = false
      }
    },
    async content(file: SessionFileRecord) {
      return http.get<SessionFileContent>(file.content_url)
    },
    async reanalyze(file: SessionFileRecord) {
      if (this.reanalyzing[file.id]) return this.items.find((item) => item.id === file.id) ?? file
      const sessionId = file.session_id || this.sessionId
      this.reanalyzing[file.id] = true
      try {
        const data = await http.post<{ file: SessionFileRecord }>(
          `/api/session-files/${encodeURIComponent(file.id)}/reanalyze`,
        )
        if (this.sessionId === sessionId) {
          this.items = this.items.map((item) => item.id === file.id ? data.file : item)
        }
        return data.file
      } finally {
        delete this.reanalyzing[file.id]
      }
    },
    async renderPreview(file: SessionFileRecord) {
      const existing = inFlightPreviewRenders.get(file.id)
      if (existing) return existing

      const sessionId = file.session_id || this.sessionId
      this.renderingPreviews[file.id] = true
      const request = (async () => {
        const data = await http.post<{ file: SessionFileRecord }>(
          `/api/session-files/${encodeURIComponent(file.id)}/render-preview`,
        )
        let rendered = data.file
        for (let attempt = 0; rendered.layout_preview_status === 'pending' && attempt < PREVIEW_POLL_ATTEMPTS; attempt += 1) {
          await wait(PREVIEW_POLL_INTERVAL_MS)
          const listed = await http.get<{ files: SessionFileRecord[] }>(
            `/api/sessions/${encodeURIComponent(sessionId)}/files`,
          )
          rendered = listed.files.find((item) => item.id === file.id) ?? rendered
        }
        if (rendered.layout_preview_status === 'pending') {
          throw new Error('Office 版式预览生成超时，请稍后重试')
        }
        return rendered
      })().then((rendered) => {
        if (this.sessionId === sessionId) {
          this.items = this.items.map((item) => item.id === file.id ? rendered : item)
        }
        return rendered
      })
      inFlightPreviewRenders.set(file.id, request)
      try {
        return await request
      } finally {
        if (inFlightPreviewRenders.get(file.id) === request) {
          inFlightPreviewRenders.delete(file.id)
        }
        delete this.renderingPreviews[file.id]
      }
    },
    restoreTransfers(transfers: SessionFileTransferRecord[]) {
      this.transfers = transfers
      const states: Record<string, SessionFileTransferState> = {}
      for (const transfer of transfers) {
        if (!transfer.file_id || states[transfer.file_id]) continue
        states[transfer.file_id] = {
          status: stateFromTransfer(transfer),
          requestId: transfer.request_id || '',
          error: transfer.error || '',
          result: transfer,
        }
      }
      this.transferStates = states
    },
    async transfer(file: SessionFileRecord, input: SessionFileTransferInput) {
      const current = this.transferStates[file.id]
      if (current && ['submitting', 'waiting_confirm', 'running'].includes(current.status)) return current.result

      const sessionId = file.session_id || this.sessionId
      const nextRequestId = requestId()
      this.transferStates[file.id] = {
        status: 'submitting',
        requestId: nextRequestId,
        error: '',
        result: null,
      }
      try {
        const data = await http.post<{
          ok: boolean
          transfer: SessionFileTransferRecord
          message?: string
        }>(
          `/api/sessions/${encodeURIComponent(sessionId)}/files/${encodeURIComponent(file.id)}/transfers`,
          { ...input, request_id: nextRequestId },
        )
        const result = {
          ...data.transfer,
          message: data.transfer?.message || data.message || '',
        }
        if (this.sessionId === sessionId) {
          this.transfers = [result, ...this.transfers.filter((item) => item.id !== result.id)]
          this.transferStates[file.id] = {
            status: stateFromTransfer(result),
            requestId: nextRequestId,
            error: result.error || '',
            result,
          }
        }
        return result
      } catch (error) {
        if (this.sessionId === sessionId) {
          this.transferStates[file.id] = {
            status: 'failed',
            requestId: nextRequestId,
            error: errorMessage(error),
            result: null,
          }
        }
        throw error
      }
    },
    async remove(file: SessionFileRecord) {
      await http.delete<{ ok: boolean }>(`/api/session-files/${encodeURIComponent(file.id)}`)
      this.items = this.items.filter((item) => item.id !== file.id)
      delete this.transferStates[file.id]
      this.transfers = this.transfers.filter((item) => item.file_id !== file.id)
    },
    reset() {
      this.sessionId = ''
      this.items = []
      this.error = ''
      this.loading = false
      this.uploading = false
      this.reanalyzing = {}
      this.renderingPreviews = {}
      this.transfers = []
      this.transferStates = {}
    },
  },
})
