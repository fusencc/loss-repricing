export interface RecordIdCopyMessage {
  type: 'success' | 'error'
  message: string
  duration: number
}

// 当前服务器页面为 HTTP：点击时同步选中文本并执行浏览器复制命令，避免依赖仅安全上下文可用的 Clipboard API。
// 按传入文本复制；命令确认写入才显示成功，临时文本始终清理，并恢复弹窗焦点与原选区。
export async function copyRecordId(value: string, label: string,
  document: Document): Promise<RecordIdCopyMessage> {
  if (!value) return { type: 'error', message: `${label}未记录，无法复制。`, duration: 3000 }
  const active = document.activeElement as HTMLElement | null
  const selection = document.getSelection()
  const ranges = selection ? Array.from({ length: selection.rangeCount }, (_, index) => selection.getRangeAt(index).cloneRange()) : []
  const text = document.createElement('textarea')
  text.value = value
  text.readOnly = true
  text.tabIndex = -1
  text.setAttribute('aria-hidden', 'true')
  Object.assign(text.style, { position: 'fixed', top: '0', left: '0', width: '1px', height: '1px', opacity: '0' })
  try {
    // 放在当前弹窗内部，避免焦点约束把选区移回按钮，导致 HTTP 页面复制失败。
    ;(active?.closest('[role="dialog"]') ?? document.body).appendChild(text)
    text.focus({ preventScroll: true })
    text.select()
    text.setSelectionRange(0, value.length)
    if (!document.execCommand('copy')) throw new Error('Browser did not confirm copy')
    return { type: 'success', message: `${label}已复制。`, duration: 2000 }
  } catch {
    return { type: 'error', message: `${label}复制失败，请选中文本手动复制。`, duration: 3000 }
  } finally {
    text.remove()
    active?.focus({ preventScroll: true })
    if (selection) {
      selection.removeAllRanges()
      for (const range of ranges) selection.addRange(range)
    }
  }
}
