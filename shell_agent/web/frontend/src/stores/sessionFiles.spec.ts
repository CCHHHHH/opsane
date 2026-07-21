import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { http } from '../api/http'
import {
  type SessionFileRecord,
  type SessionFileTransferRecord,
  useSessionFilesStore,
} from './sessionFiles'

const file: SessionFileRecord = {
  id: 'file-1', session_id: 'session-1', name: 'app.jar', media_type: 'application/java-archive',
  extension: '.jar', kind: 'package', preview_type: 'none', size: 1024, sha256: 'abc123',
  parse_status: 'metadata_only', parse_error: '', metadata: {}, created_at: '2026-07-16T10:00:00',
  preview_url: '/preview', content_url: '/content', download_url: '/download',
}

function transfer(status = 'success'): SessionFileTransferRecord {
  return {
    id: 'transfer-1', request_id: 'request-1', session_id: 'session-1', file_id: 'file-1',
    file_name: 'app.jar', target: 'dev-01', remote_dir: '/tmp/uploads', remote_name: 'app.jar',
    remote_path: '/tmp/uploads/app.jar', size: 1024, sha256: 'abc123', remote_size: 1024,
    remote_sha256: 'abc123', status, error: '', message: '', created_at: '2026-07-16T10:00:00',
  }
}

describe('session file transfers store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('submits one idempotent request and blocks a second click while it is in flight', async () => {
    const store = useSessionFilesStore()
    store.sessionId = 'session-1'
    let resolveRequest!: (value: { ok: boolean; transfer: SessionFileTransferRecord }) => void
    const pending = new Promise<{ ok: boolean; transfer: SessionFileTransferRecord }>((resolve) => {
      resolveRequest = resolve
    })
    const post = vi.spyOn(http, 'post').mockReturnValue(pending)
    const input = { target: 'dev-01', remote_dir: '/tmp/uploads', remote_name: 'app.jar', overwrite: false }

    const first = store.transfer(file, input)
    const second = store.transfer(file, input)

    expect(post).toHaveBeenCalledTimes(1)
    expect(store.transferStateForFile(file.id)?.status).toBe('submitting')
    const body = post.mock.calls[0][1] as Record<string, unknown>
    expect(body.target).toBe('dev-01')
    expect(body.request_id).toMatch(/^transfer-/)
    expect(post.mock.calls[0][0]).toBe('/api/sessions/session-1/files/file-1/transfers')

    resolveRequest({ ok: true, transfer: transfer('running') })
    await Promise.all([first, second])
    expect(store.transferStateForFile(file.id)?.status).toBe('running')
  })

  it('submits only one multipart upload while rapid paste and drop gestures overlap', async () => {
    const store = useSessionFilesStore()
    store.sessionId = 'session-1'
    const localFile = new File(['release'], 'release.jar', { type: 'application/java-archive' })
    let resolveRequest!: (value: { files: SessionFileRecord[] }) => void
    const pending = new Promise<{ files: SessionFileRecord[] }>((resolve) => {
      resolveRequest = resolve
    })
    const post = vi.spyOn(http, 'post').mockReturnValue(pending)
    vi.spyOn(store, 'load').mockResolvedValue()

    const first = store.upload('session-1', [localFile])
    const second = store.upload('session-1', [localFile])

    expect(post).toHaveBeenCalledTimes(1)
    expect(store.uploading).toBe(true)
    await expect(second).resolves.toEqual([])
    resolveRequest({ files: [file] })
    await first
    expect(store.uploading).toBe(false)
  })

  it('does not let a late upload response restore the previous conversation', async () => {
    const store = useSessionFilesStore()
    store.sessionId = 'session-1'
    const localFile = new File(['release'], 'release.jar', { type: 'application/java-archive' })
    let resolveRequest!: (value: { files: SessionFileRecord[] }) => void
    const pending = new Promise<{ files: SessionFileRecord[] }>((resolve) => {
      resolveRequest = resolve
    })
    vi.spyOn(http, 'post').mockReturnValue(pending)
    const load = vi.spyOn(store, 'load').mockResolvedValue()

    const upload = store.upload('session-1', [localFile])
    store.sessionId = 'session-2'
    store.items = [{ ...file, id: 'file-2', session_id: 'session-2' }]
    resolveRequest({ files: [file] })
    await upload

    expect(load).not.toHaveBeenCalled()
    expect(store.sessionId).toBe('session-2')
    expect(store.items.map((item) => item.session_id)).toEqual(['session-2'])
  })

  it('reanalyzes one legacy Office file once and replaces it in the active session', async () => {
    const store = useSessionFilesStore()
    const legacy = {
      ...file,
      id: 'legacy-doc',
      name: '部署说明.doc',
      extension: '.doc',
      kind: 'document',
      parse_status: 'metadata_only',
      parse_error: '旧版本仅保存元数据',
    }
    const ready = {
      ...legacy,
      preview_type: 'text' as const,
      parse_status: 'ready',
      parse_error: '',
      metadata: { converted_format: 'docx' },
    }
    store.sessionId = 'session-1'
    store.items = [legacy]
    let resolveRequest!: (value: { file: SessionFileRecord }) => void
    const pending = new Promise<{ file: SessionFileRecord }>((resolve) => {
      resolveRequest = resolve
    })
    const post = vi.spyOn(http, 'post').mockReturnValue(pending)

    const first = store.reanalyze(legacy)
    const second = store.reanalyze(legacy)

    expect(post).toHaveBeenCalledTimes(1)
    expect(post).toHaveBeenCalledWith('/api/session-files/legacy-doc/reanalyze')
    expect(store.reanalyzing['legacy-doc']).toBe(true)
    await expect(second).resolves.toEqual(legacy)
    resolveRequest({ file: ready })
    await first

    expect(store.items).toEqual([ready])
    expect(store.reanalyzing['legacy-doc']).toBeUndefined()
  })

  it('does not let a late legacy reanalysis response replace another session files', async () => {
    const store = useSessionFilesStore()
    const legacy = { ...file, id: 'legacy-doc', name: '说明.doc', extension: '.doc' }
    const ready = { ...legacy, preview_type: 'text' as const, parse_status: 'ready' }
    store.sessionId = 'session-1'
    store.items = [legacy]
    let resolveRequest!: (value: { file: SessionFileRecord }) => void
    vi.spyOn(http, 'post').mockReturnValue(new Promise((resolve) => {
      resolveRequest = resolve
    }))

    const request = store.reanalyze(legacy)
    store.sessionId = 'session-2'
    store.items = [{ ...file, id: 'file-2', session_id: 'session-2' }]
    resolveRequest({ file: ready })
    await request

    expect(store.items.map((item) => item.session_id)).toEqual(['session-2'])
  })

  it('renders one formatted Office preview request while repeated preview clicks overlap', async () => {
    const store = useSessionFilesStore()
    const officeFile = {
      ...file,
      id: 'office-doc',
      name: '部署说明.doc',
      extension: '.doc',
      kind: 'document',
      preview_type: 'text' as const,
      parse_status: 'ready',
      layout_preview_status: 'none',
    }
    const formatted = {
      ...officeFile,
      preview_type: 'pdf' as const,
      layout_preview_status: 'ready',
      layout_preview_error: '',
      layout_preview_size: 4096,
    }
    store.sessionId = 'session-1'
    store.items = [officeFile]
    let resolveRequest!: (value: { file: SessionFileRecord }) => void
    const pending = new Promise<{ file: SessionFileRecord }>((resolve) => {
      resolveRequest = resolve
    })
    const post = vi.spyOn(http, 'post').mockReturnValue(pending)

    const first = store.renderPreview(officeFile)
    const second = store.renderPreview(officeFile)

    expect(post).toHaveBeenCalledTimes(1)
    expect(post).toHaveBeenCalledWith('/api/session-files/office-doc/render-preview')
    expect(store.renderingPreviews['office-doc']).toBe(true)
    resolveRequest({ file: formatted })

    await expect(first).resolves.toEqual(formatted)
    await expect(second).resolves.toEqual(formatted)
    expect(store.items).toEqual([formatted])
    expect(store.renderingPreviews['office-doc']).toBeUndefined()
  })

  it('waits for a formatted preview already being generated in another browser tab', async () => {
    vi.useFakeTimers()
    try {
      const store = useSessionFilesStore()
      const officeFile = {
        ...file,
        id: 'office-shared',
        name: '共享说明.docx',
        extension: '.docx',
        kind: 'document',
        preview_type: 'text' as const,
        parse_status: 'ready',
        layout_preview_status: 'none',
      }
      const pending = { ...officeFile, layout_preview_status: 'pending' }
      const ready = {
        ...officeFile,
        preview_type: 'pdf' as const,
        layout_preview_status: 'ready',
      }
      store.sessionId = 'session-1'
      store.items = [officeFile]
      vi.spyOn(http, 'post').mockResolvedValue({ file: pending })
      const get = vi.spyOn(http, 'get').mockResolvedValue({ files: [ready] })

      const request = store.renderPreview(officeFile)
      await vi.advanceTimersByTimeAsync(500)

      await expect(request).resolves.toEqual(ready)
      expect(get).toHaveBeenCalledWith('/api/sessions/session-1/files')
      expect(store.items).toEqual([ready])
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not let a late formatted preview response replace another session files', async () => {
    const store = useSessionFilesStore()
    const officeFile = {
      ...file,
      id: 'office-doc',
      name: '说明.xlsx',
      extension: '.xlsx',
      session_id: 'session-1',
      preview_type: 'text' as const,
    }
    const formatted = {
      ...officeFile,
      preview_type: 'pdf' as const,
      layout_preview_status: 'ready',
    }
    store.sessionId = 'session-1'
    store.items = [officeFile]
    let resolveRequest!: (value: { file: SessionFileRecord }) => void
    vi.spyOn(http, 'post').mockReturnValue(new Promise((resolve) => {
      resolveRequest = resolve
    }))

    const request = store.renderPreview(officeFile)
    store.sessionId = 'session-2'
    store.items = [{ ...file, id: 'file-2', session_id: 'session-2' }]
    resolveRequest({ file: formatted })
    await request

    expect(store.items.map((item) => item.session_id)).toEqual(['session-2'])
  })

  it('restores a running transfer after loading the session', async () => {
    vi.spyOn(http, 'get').mockImplementation(async (path) => {
      if (path.endsWith('/file-transfers')) return { transfers: [transfer('running')] }
      return { files: [file] }
    })
    const store = useSessionFilesStore()

    await store.load('session-1')

    expect(store.items).toEqual([file])
    expect(store.transferStateForFile(file.id)?.status).toBe('running')
    expect(store.transferStateForFile(file.id)?.result?.remote_path).toBe('/tmp/uploads/app.jar')
  })

  it.each([
    ['waiting_confirm', 'waiting_confirm'],
    ['cancelled', 'canceled'],
  ] as const)('keeps conversational transfer state %s distinct from a failed upload', (remoteStatus, expected) => {
    const store = useSessionFilesStore()

    store.restoreTransfers([transfer(remoteStatus)])

    expect(store.transferStateForFile(file.id)?.status).toBe(expected)
    expect(store.transferStateForFile(file.id)?.error).toBe('')
  })

  it('records terminal success and exposes the verified remote checksum', async () => {
    const store = useSessionFilesStore()
    store.sessionId = 'session-1'
    vi.spyOn(http, 'post').mockResolvedValue({ ok: true, transfer: transfer('success') })

    await store.transfer(file, {
      target: 'dev-01', remote_dir: '/tmp/uploads', remote_name: 'app.jar', overwrite: false,
    })

    expect(store.transferStateForFile(file.id)?.status).toBe('success')
    expect(store.transferStateForFile(file.id)?.result?.remote_sha256).toBe('abc123')
  })
})
