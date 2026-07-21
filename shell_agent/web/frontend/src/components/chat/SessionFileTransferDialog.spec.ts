import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { ServerRecord } from '../../api/protocol'
import type { SessionFileRecord, SessionFileTransferState } from '../../stores/sessionFiles'
import SessionFileTransferDialog from './SessionFileTransferDialog.vue'

const file: SessionFileRecord = {
  id: 'file-1', session_id: 'session-1', name: 'bedcare-mock.jar', media_type: 'application/java-archive',
  extension: '.jar', kind: 'package', preview_type: 'none', size: 18 * 1024 * 1024, sha256: 'abcdef1234567890',
  parse_status: 'metadata_only', parse_error: '', metadata: {}, created_at: '2026-07-16T10:00:00',
  preview_url: '/preview', content_url: '/content', download_url: '/download',
}
const servers: ServerRecord[] = [
  { alias: 'dev-01', host: '10.0.0.1', env: 'dev' },
  { alias: 'prod-01', host: '10.0.0.2', env: 'prod' },
]

function mountDialog(state?: SessionFileTransferState) {
  return mount(SessionFileTransferDialog, {
    props: { file, servers, state },
    global: { stubs: { Teleport: true } },
  })
}

describe('SessionFileTransferDialog', () => {
  it('validates the destination and emits an explicit transfer request', async () => {
    const wrapper = mountDialog()
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('submit')).toBeUndefined()
    expect(wrapper.text()).toContain('请选择目标服务器')

    await wrapper.get('select').setValue('dev-01')
    const inputs = wrapper.findAll('input.input')
    await inputs[0].setValue('/data/app/uploads')
    await inputs[1].setValue('release.jar')
    await wrapper.get('form').trigger('submit')

    expect(wrapper.emitted('submit')?.[0]?.[0]).toEqual({
      target: 'dev-01', remote_dir: '/data/app/uploads', remote_name: 'release.jar', overwrite: false,
    })
    expect(wrapper.text()).toContain('dev-01:/data/app/uploads/release.jar')
  })

  it('shows an indeterminate SFTP progress state and prevents duplicate submission', async () => {
    const wrapper = mountDialog({
      status: 'running', requestId: 'request-1', error: '',
      result: {
        id: 'transfer-1', request_id: 'request-1', session_id: 'session-1', file_id: 'file-1',
        file_name: file.name, target: 'dev-01', remote_dir: '/tmp', remote_name: file.name,
        remote_path: `/tmp/${file.name}`, size: file.size, sha256: file.sha256, status: 'running',
        error: '', message: '', created_at: '2026-07-16T10:00:00',
      },
    })

    expect(wrapper.text()).toContain('正在通过 SFTP 上传')
    expect(wrapper.get('section[role="dialog"]').attributes('aria-busy')).toBe('true')
    expect(wrapper.findAll('button').every((button) => button.attributes('disabled') !== undefined)).toBe(true)
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('submit')).toBeUndefined()
  })

  it('shows the remote path and verified checksum after success', () => {
    const wrapper = mountDialog({
      status: 'success', requestId: 'request-1', error: '',
      result: {
        id: 'transfer-1', request_id: 'request-1', session_id: 'session-1', file_id: 'file-1',
        file_name: file.name, target: 'dev-01', remote_dir: '/data/app', remote_name: file.name,
        remote_path: `/data/app/${file.name}`, size: file.size, sha256: file.sha256,
        remote_size: file.size, remote_sha256: 'verified-sha256', status: 'success', error: '', message: '',
        created_at: '2026-07-16T10:00:00', completed_at: '2026-07-16T10:00:03',
      },
    })

    expect(wrapper.text()).toContain('文件传输完成')
    expect(wrapper.text()).toContain(`dev-01:/data/app/${file.name}`)
    expect(wrapper.text()).toContain('verified-sha256')
  })
})
