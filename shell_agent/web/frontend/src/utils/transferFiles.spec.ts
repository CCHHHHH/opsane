import { describe, expect, it } from 'vitest'

import { filesFromTransfer, transferHasFiles } from './transferFiles'

function transfer(payload: Partial<DataTransfer>): DataTransfer {
  return payload as DataTransfer
}

describe('transfer file helpers', () => {
  it('prefers the FileList and does not duplicate clipboard files from items', () => {
    const file = new File(['hello'], 'clipboard.txt', { type: 'text/plain' })
    const data = transfer({
      files: [file] as unknown as FileList,
      items: [{ kind: 'file', getAsFile: () => file }] as unknown as DataTransferItemList,
      types: ['Files'],
    })

    expect(filesFromTransfer(data)).toEqual([file])
    expect(transferHasFiles(data)).toBe(true)
  })

  it('falls back to clipboard items and ignores text plus empty file items', () => {
    const image = new File(['png'], 'clipboard.png', { type: 'image/png' })
    const data = transfer({
      files: [] as unknown as FileList,
      items: [
        { kind: 'string', getAsFile: () => null },
        { kind: 'file', getAsFile: () => null },
        { kind: 'file', getAsFile: () => image },
      ] as unknown as DataTransferItemList,
      types: ['text/plain', 'Files'],
    })

    expect(filesFromTransfer(data)).toEqual([image])
    expect(transferHasFiles(data)).toBe(true)
  })

  it('leaves ordinary copied text to the textarea', () => {
    const data = transfer({
      files: [] as unknown as FileList,
      items: [{ kind: 'string', getAsFile: () => null }] as unknown as DataTransferItemList,
      types: ['text/plain'],
    })

    expect(filesFromTransfer(data)).toEqual([])
    expect(transferHasFiles(data)).toBe(false)
  })
})
