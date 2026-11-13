import { ref, onUnmounted, getCurrentInstance } from "vue"
import { request } from '@/utils/requests.ts'
import { buildApiUrl } from '@/utils/apiBase'
import type { RagSource } from '@/types/rag'

export interface StreamingMessage {
  type: 'chunk' | 'complete' | 'error'
  content: string
  fullResponse?: string
  sources?: RagSource[]
  timestamp?: string
  total_length?: number
  error?: string
}

export type StreamTerminal = 'completed' | 'failed' | 'cancelled' | 'interrupted' | 'unknown'
export type StreamInterruptedReason = 'eof' | 'network' | 'protocol'

export interface StreamingOptions {
  characterId?: string
  /** 自定义流式端点相对路径（如 RAG：/api/v1/rag/query）；不传则使用普通 Chat 端点 */
  url?: string
  /** 自定义请求体构造器；不传则使用 { message } */
  buildBody?: (message: string) => any
  onChunk?: (chunk: string, accumulated: string) => void
  onComplete?: (fullResponse: string, sources: RagSource[]) => void
  onError?: (error: Error) => void
  /** 用户主动取消；组件卸载不会触发该回调 */
  onCancelled?: (partialText: string) => void
  /** EOF、网络读取异常或协议错误；部分文本仅供当前 UI 展示 */
  onInterrupted?: (partialText: string, reason: StreamInterruptedReason) => void
}

export function useStreamingChat(options: StreamingOptions) {
  const isStreaming = ref(false)
  const accumulatedText = ref('')
  const error = ref<string | null>(null)
  const abortController = ref<AbortController | null>(null)

  let requestGeneration = 0
  let activeGeneration = 0
  let terminal: StreamTerminal | null = null
  let cancelReason: 'user' | 'unmount' | null = null

  const isCurrent = (generation: number) => (
    generation === requestGeneration && generation === activeGeneration
  )

  const safeCallback = (fn: (() => void) | undefined) => {
    if (!fn) return
    try {
      fn()
    } catch (callbackError) {
      // 业务回调异常必须与 SSE 数据解析异常隔离：记录，但不改变终态。
      console.error('流式终态回调执行失败：', callbackError)
    }
  }

  const settle = (status: StreamTerminal, generation: number): boolean => {
    if (!isCurrent(generation) || terminal !== null) return false
    terminal = status
    return true
  }

  const finishInterrupted = (
    generation: number,
    reason: StreamInterruptedReason,
    message: string,
  ) => {
    if (!settle('interrupted', generation)) return
    error.value = message
    safeCallback(() => options.onInterrupted?.(accumulatedText.value, reason))
  }

  const finishProtocolError = (generation: number) => {
    if (!settle('unknown', generation)) return
    error.value = '流式响应协议错误'
    safeCallback(() => options.onInterrupted?.(accumulatedText.value, 'protocol'))
  }

  const sendMessage = async (message: string): Promise<void> => {
    if (isStreaming.value) {
      throw new Error('已有请求在进行中')
    }

    const generation = ++requestGeneration
    activeGeneration = generation
    terminal = null
    cancelReason = null

    isStreaming.value = true
    error.value = null
    accumulatedText.value = ''
    const controller = new AbortController()
    abortController.value = controller

    const handleData = (data: StreamingMessage) => {
      if (terminal !== null) return

      switch (data.type) {
        case 'chunk':
          if (typeof data.content !== 'string') {
            finishProtocolError(generation)
            return
          }
          accumulatedText.value += data.content
          safeCallback(() => options.onChunk?.(data.content, accumulatedText.value))
          break
        case 'complete':
          if (!settle('completed', generation)) return
          safeCallback(() => options.onComplete?.(
            typeof data.fullResponse === 'string' ? data.fullResponse : accumulatedText.value,
            Array.isArray(data.sources) ? data.sources : []
          ))
          break
        case 'error':
          if (!settle('failed', generation)) return
          error.value = typeof data.error === 'string' && data.error ? data.error : '流式响应错误'
          safeCallback(() => options.onError?.(new Error(error.value || '未知错误')))
          break
        default:
          // 未知帧不改变终态；若之后没有合法 complete/error，EOF 会按 INTERRUPTED 处理。
          break
      }
    }

    const handleLine = (line: string) => {
      if (!isCurrent(generation) || terminal !== null) return

      let data: StreamingMessage
      try {
        data = JSON.parse(line)
      } catch {
        finishProtocolError(generation)
        return
      }
      if (!data || typeof data !== 'object' || typeof data.type !== 'string') {
        finishProtocolError(generation)
        return
      }
      handleData(data)
    }

    try {
      const baseURL = (request.defaults.baseURL || '').replace(/\/+$/, '')
      const relPath = options.url || `/api/v1/characters/${options.characterId}/speak/stream`
      const url = buildApiUrl(baseURL, relPath)
      const token = localStorage.getItem('access_token')

      const resp = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/x-ndjson',
          ...(token ? { Authorization: `Bearer ${token}` } : {})
        },
        body: JSON.stringify(options.buildBody ? options.buildBody(message) : { message }),
        signal: controller.signal
      })

      if (!resp.ok) {
        let msg = `请求失败 (${resp.status})`
        try {
          const t = await resp.text()
          const j = JSON.parse(t)
          msg = j.message || j.detail || msg
        } catch {
          /* ignore body parse error */
        }
        if (settle('failed', generation)) {
          error.value = msg
          safeCallback(() => options.onError?.(new Error(msg)))
        }
        return
      }

      const decoder = new TextDecoder('utf-8')
      let buffer = ''
      let reader: ReadableStreamDefaultReader<Uint8Array> | null = null
      let readerFinished = false

      try {
        if (resp.body && typeof (resp.body as ReadableStream<Uint8Array>).getReader === 'function') {
          reader = (resp.body as ReadableStream<Uint8Array>).getReader()
          while (terminal === null && isCurrent(generation)) {
            const { done, value } = await reader.read()
            if (done) {
              readerFinished = true
              break
            }
            buffer += decoder.decode(value, { stream: true })
            const lines = buffer.split('\n')
            buffer = lines.pop() ?? ''
            for (const line of lines) {
              if (!line.trim()) continue
              handleLine(line)
              if (terminal !== null) break
            }
          }
          if (terminal === null && buffer.trim()) {
            // 合法 complete 即使没有 final newline 也必须成功；被截断的 JSON 进入 UNKNOWN。
            handleLine(buffer.trim())
          }
        } else {
          // 兜底：部分环境把响应缓冲为字符串
          const text = await resp.text()
          for (const line of text.split('\n')) {
            if (!line.trim()) continue
            handleLine(line)
            if (terminal !== null) break
          }
        }
      } finally {
        if (reader) {
          // 自然 EOF 不额外 cancel；只有提前终止（complete/error/protocol/abort/网络失败）才取消读取。
          if (!readerFinished) {
            try {
              // 不使用无界 await：取消失败不能阻塞终态清理。
              const cancelPromise = reader.cancel()
              if (cancelPromise && typeof cancelPromise.catch === 'function') {
                cancelPromise.catch(cancelError => {
                  console.error('取消流 reader 失败：', cancelError)
                })
              }
            } catch (cancelError) {
              console.error('取消流 reader 失败：', cancelError)
            }
          }
          try {
            reader.releaseLock()
          } catch (releaseError) {
            console.error('释放流 reader 失败：', releaseError)
          }
        }
      }

      if (terminal === null) {
        finishInterrupted(generation, 'eof', '连接已结束，但未收到 complete 终态')
      }
    } catch (err: any) {
      if (err?.name === 'AbortError' || controller.signal.aborted) {
        if (terminal === null) {
          if (settle('cancelled', generation) && cancelReason === 'user') {
            error.value = '请求被取消'
            safeCallback(() => options.onCancelled?.(accumulatedText.value))
          }
        }
      } else {
        error.value = err?.message || '流式请求失败'
        finishInterrupted(generation, 'network', error.value)
      }
    } finally {
      if (isCurrent(generation)) {
        isStreaming.value = false
        if (abortController.value === controller) {
          abortController.value = null
        }
      }
    }
  }

  const cancel = (reason: 'user' | 'unmount' = 'user') => {
    const controller = abortController.value
    if (!controller) return
    if (terminal === null && settle('cancelled', activeGeneration)) {
      cancelReason = reason
      if (reason === 'user') {
        error.value = '请求被取消'
        safeCallback(() => options.onCancelled?.(accumulatedText.value))
      }
    }
    // 允许取消后立刻启动新请求；旧请求 finally 通过 generation guard 不会清理新请求。
    isStreaming.value = false
    controller.abort()
    if (abortController.value === controller) {
      abortController.value = null
    }
  }

  if (getCurrentInstance()) {
    onUnmounted(() => {
      cancel('unmount')
    })
  }

  return {
    sendMessage,
    cancel,
    isStreaming,
    accumulatedText,
    error
  }
}
