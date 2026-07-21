/**
 * Return the files carried by a clipboard or drag-and-drop payload.
 *
 * Browsers normally expose dropped files through `files`, while clipboard
 * implementations sometimes only expose them through `items`. Prefer the
 * former so the same file is never returned twice.
 */
export function filesFromTransfer(transfer?: DataTransfer | null): File[] {
  if (!transfer) return []

  const directFiles = Array.from(transfer.files ?? [])
  if (directFiles.length) return directFiles

  return Array.from(transfer.items ?? []).flatMap((item) => {
    if (item.kind !== 'file') return []
    const file = item.getAsFile()
    return file ? [file] : []
  })
}

/** Detect a file drag before the browser makes the FileList readable. */
export function transferHasFiles(transfer?: DataTransfer | null): boolean {
  if (!transfer) return false
  if (Array.from(transfer.types ?? []).includes('Files')) return true
  if (Array.from(transfer.files ?? []).length > 0) return true
  return Array.from(transfer.items ?? []).some((item) => item.kind === 'file')
}
