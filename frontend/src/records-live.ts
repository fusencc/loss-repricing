// SSE（服务器发送事件）只接收已落库日志。浏览器负责断线重连，重连首帧为最新完整视图。
export function connectRecords<T>(department: string, receive: (data: T) => void, failed: () => void) {
  const query = new URLSearchParams(department ? { department } : {})
  const source = new EventSource(`/api/records/stream?${query}`)
  source.onmessage = event => {
    let data: T
    try {
      data = JSON.parse(event.data) as T
    } catch {
      failed()
      return
    }
    receive(data)
  }
  source.onerror = failed
  return () => source.close()
}
