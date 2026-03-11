import {ref, onMounted, onUnmounted} from 'vue'

export interface WebSocketOptions {
  url: string
  autoConnect?: boolean
  onMessage?: (event: MessageEvent) => void
  onOpen?: (event: Event) => void
  onClose?: (event: CloseEvent) => void
  onError?: (event: Event) => void
  reconnectAttempts?: number;
  reconnectInterval?: number;
}

export function useWedSocket(options: WebSocketOptions) {
  const socket = ref<WebSocket | null>(null)
  const isConnected = ref(false)
  const messages = ref<any[]>([])
  const error = ref<string | null>(null)
  const reconnectCount = ref(0)

  const connect = () => {
    try {
      socket.value = new WebSocket(options.url)

      socket.value.onopen = (event) => {
        isConnected.value = true
        error.value = null
        reconnectCount.value = 0
        options.onOpen?.(event)
        console.log('WebSocket连接成功')
      }

      socket.value.onmessage = (event) => {
        messages.value.push({
          data: event.data,
          timestamp: new Date().toISOString(),
        })
        options.onMessage?.(event)
      }

      socket.value.onclose = (event) => {
        isConnected.value = false
        options.onClose?.(event)

        // 自动重连逻辑
        if (reconnectCount.value < (options.reconnectAttempts || 3)) {
          reconnectCount.value++
          setTimeout(() => {
            console.log(`尝试重连(${reconnectCount.value}/${options.reconnectAttempts || 3})`)
            connect()
          },
          options.reconnectInterval || 3000)
        }
      }

      socket.value.onerror = (event) => {
        error.value = 'WebSocket连接错误'
        options.onError?.(event)
        console.error('WebSocket错误：', event)
      }
    } catch (err) {
      error.value = `连接失败：${err}`
    }
  }

  const send = (data: any) => {
    if (socket.value && isConnected.value) {
      if (typeof data !== 'string') {
        data = JSON.stringify(data)
      }
      socket.value.send(data)
      return true
    }
    return false
  }

  const disconnect = () => {
    if (socket.value) {
      socket.value.close()
      socket.value = null
      isConnected.value = false
    }
  }

  // 自动连接
  if (options.autoConnect !== false) {
    connect()
  }

  // 组件卸载时断开连接
  onUnmounted(() => {
    disconnect()
  })

  return {
    socket,
    isConnected,
    messages,
    error,
    connect,
    disconnect,
    send
  }
}
