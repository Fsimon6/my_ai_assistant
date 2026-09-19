import { ref, onUnmounted } from "vue"
import { request } from '@/utils/requests.ts'

export interface StreamingMessage {
  type: 'chunk' | 'complete' | 'error'
  content: string
  fullResponse?: string
  timestamp?: string
  total_length?: number
  error?: string
}

export interface StreamingOptions {
  characterId?: string
  /** 自定义流式端点相对路径（如 RAG：/api/v1/rag/query）；不传则使用普通 Chat 端点 */
  url?: string
  /** 自定义请求体构造器；不传则使用 { message } */
  buildBody?: (message: string) => any
  onChunk?: (chunk: string, accumulated: string) => void
  onComplete?: (fullResponse: string) => void
  onError?: (error: Error) => void
}

export function useStreamingChat(options: StreamingOptions) {
  const isStreaming = ref(false)
  const accumulatedText = ref('')
  const error = ref<string | null>(null)
  const abortController = ref<AbortController | null>(null)

  const sendMessage = async (message: string): Promise<void> => {
    if (isStreaming.value) {
      throw new Error('已有请求在进行中')
    }

    isStreaming.value = true
    error.value = null
    accumulatedText.value = ''
    abortController.value = new AbortController()

    try {
      // 浏览器 XHR 不支持 responseType:'stream'（axios 会把整段 ndjson 缓冲成字符串），
      // 因此这里改用原生 fetch + ReadableStream 实现真正的逐 token 流式读取。
      // URL 拼接复刻 axios 的 combineURLs：相对路径优先走同源（Vite 代理 /api）。
      const baseURL = (request.defaults.baseURL || '').replace(/\/+$/, '')
      const relPath = options.url || `/api/v1/characters/${options.characterId}/speak/stream`
      const url = /^https?:\/\//i.test(relPath)
        ? relPath
        : (baseURL ? `${baseURL}/${relPath.replace(/^\/+/, '')}` : relPath)
      const token = localStorage.getItem('access_token')

      const resp = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/x-ndjson',
          ...(token ? { Authorization: `Bearer ${token}` } : {})
        },
        body: JSON.stringify(options.buildBody ? options.buildBody(message) : { message }),
        signal: abortController.value?.signal
      })

      if (!resp.ok) {
        let msg = `请求失败 (${resp.status})`
        try {
          const t = await resp.text()
          const j = JSON.parse(t)
          msg = j.message || j.detail || msg
        } catch {
          /* ignore parse error */
        }
        throw new Error(msg)
      }

      const decoder = new TextDecoder('utf-8')
      let buffer = ''
      let completed = false
      let errored = false

      const handleLine = (line: string) => {
        const data: StreamingMessage = JSON.parse(line)
        switch (data.type) {
          case 'chunk':
            accumulatedText.value += data.content
            options.onChunk?.(data.content, accumulatedText.value)
            break
          case 'complete':
            completed = true
            options.onComplete?.(data.fullResponse ?? accumulatedText.value)
            break
          case 'error':
            errored = true
            error.value = data.error || '流式响应错误'
            options.onError?.(new Error(data.error || '未知错误'))
            break
        }
      }

      if (resp.body && typeof (resp.body as ReadableStream<Uint8Array>).getReader === 'function') {
        const reader = (resp.body as ReadableStream<Uint8Array>).getReader()
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() ?? ''
          for (const line of lines) {
            if (!line.trim()) continue
            try {
              handleLine(line)
            } catch (parseError) {
              console.error('解析流数据失败：', parseError, line)
            }
          }
        }
        if (buffer.trim()) {
          try {
            handleLine(buffer.trim())
          } catch (parseError) {
            console.error('解析流数据失败：', parseError, buffer)
          }
        }
      } else {
        // 兜底：部分环境把响应缓冲为字符串
        const text = await resp.text()
        for (const line of text.split('\n')) {
          if (!line.trim()) continue
          try {
            handleLine(line)
          } catch (parseError) {
            console.error('解析流数据失败：', parseError, line)
          }
        }
      }

      if (!completed && !errored) {
        if (error.value) {
          options.onError?.(new Error(error.value))
        } else {
          options.onComplete?.(accumulatedText.value)
        }
      }
    } catch (err: any) {
      if (err.name === 'AbortError') {
        error.value = '请求被取消'
      } else {
        error.value = err.message || '流式请求失败'
        options.onError?.(err)
      }
    } finally {
      isStreaming.value = false
      abortController.value = null
    }
  }

  const cancel = () => {
    if (abortController.value) {
      abortController.value.abort()
      abortController.value = null
      isStreaming.value = false
    }
  }

  onUnmounted(() => {
    cancel()
  })

  return {
    sendMessage,
    cancel,
    isStreaming,
    accumulatedText,
    error
  }
}

