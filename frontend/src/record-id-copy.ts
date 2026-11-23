export interface RecordIdCopyMessage {
  type: 'success' | 'error'
  message: string
  duration: number
}

// ID 始终按来源文本复制，不带展示前缀、不转数字；仅在浏览器确认写入后反馈成功。
// 结果交给统一消息浮层：成功 2 秒、失败 3 秒，不在商品标题下插入文本或维护额外计时器。
export async function copyRecordId(value: string, label: string,
  clipboard: Pick<Clipboard, 'writeText'> | undefined): Promise<RecordIdCopyMessage> {
  if (!value) return { type: 'error', message: `${label}未记录，无法复制。`, duration: 3000 }
  if (!clipboard) return { type: 'error', message: `${label}复制失败，请选中文本手动复制。`, duration: 3000 }
  try {
    await clipboard.writeText(value)
    return { type: 'success', message: `${label}已复制。`, duration: 2000 }
  } catch {
    return { type: 'error', message: `${label}复制失败，请选中文本手动复制。`, duration: 3000 }
  }
}
