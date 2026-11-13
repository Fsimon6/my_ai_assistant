/**
 * RAG 客户端 history 的唯一过滤入口。
 *
 * 规则：
 * - 旧服务端历史没有 streamStatus/turnStatus，必须兼容并保留；
 * - 只有 completed 或旧消息可进入；
 * - error、streaming、Excel、失败/中断/取消/未知整轮都不得进入。
 */
export interface RagHistoryMessage {
  role?: string
  content?: unknown
  isStreaming?: boolean
  isExcelQuery?: boolean
  error?: boolean
  streamStatus?: string
  turnStatus?: string
}

export function isRagHistoryMessage(message: RagHistoryMessage | null | undefined): boolean {
  if (!message) return false
  if (message.role !== 'user' && message.role !== 'assistant') return false
  if (typeof message.content !== 'string' || !message.content.trim()) return false
  if (message.isStreaming) return false
  if (message.isExcelQuery) return false
  if (message.error) return false
  if (message.streamStatus && message.streamStatus !== 'completed') return false
  if (message.turnStatus && message.turnStatus !== 'completed') return false
  return true
}

export function buildRagHistory(
  messages: RagHistoryMessage[],
  endIndex: number = messages.length,
): Array<{ role: string; content: string }> {
  // endIndex 是排除边界；负数/NaN 安全归零，越界截断。
  const safeEnd = Number.isFinite(endIndex)
    ? Math.min(Math.max(Math.trunc(endIndex), 0), messages.length)
    : 0
  return messages
    .slice(0, safeEnd)
    .filter(isRagHistoryMessage)
    .map(message => ({ role: message.role as string, content: message.content as string }))
}
