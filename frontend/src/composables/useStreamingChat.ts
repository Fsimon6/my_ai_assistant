import { ref, onUnmounted } from "vue"
import request from '@/utils/requests.ts'

export interface StreamingMessage {
  type: 'chunk' | 'complete' | 'error'
  content: string
  timestamp?: string
  total_length?: number
  error?: string
}

export interface StreamingOptions {
  characterId: string
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
      const response = await request.post(
        `/api/v1//characters/${options.characterId}/speak/stream`,
        { message },
        {
          responseType: 'stream',
          signal: abortController.value.signal,
          headers: {
            'Accept': 'application/x-ndjson'
          }
        }
      )

      // 处理流式响应
      const reader = response.data.getReader()
      const decoder = new TextDecoder('utf-8')

      while (true) {
        const { done, value } = await reader.read()

        if (done) {
          break
        }

        const chunk = decoder.decode(value)
        const lines = chunk.split('\n').filter(line => line.trim())

        for (const line of lines) {
          try {
            const data: StreamingMessage = JSON.parse(line)

            switch (data.type) {
              case "chunk":
                accumulatedText.value += data.content
                options.onChunk?.(data.content, accumulatedText.value)
                break

              case "complete":
                options.onComplete?.(accumulatedText.value)
                break

              case "error":
                error.value = data.error || '流式响应错误'
                options.onError?.(new Error(data.error || '未知错误'))
                break
            }
          } catch (parseError) {
            console.error('解析流数据失败：', parseError, line)
          }
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

