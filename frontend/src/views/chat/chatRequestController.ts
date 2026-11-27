/**
 * Chat.vue 页面级单活动生成请求控制器。
 *
 * 每个 Chat.vue setup 实例创建自己的 controller；不得做成模块级 singleton。
 * controller 只负责请求身份、活动槽位、取消/失效和 finish 所有权，不发起请求。
 */
export type ChatRequestMode = 'chat' | 'rag' | 'excel'
export type ChatRequestCancelReason = 'user' | 'unmount'

export interface ChatRequestToken {
  readonly id: number
  readonly mode: ChatRequestMode
}

type CancelHandler = (reason?: ChatRequestCancelReason) => void

interface InternalToken extends ChatRequestToken {
  mode: ChatRequestMode
  cancel: CancelHandler | null
  cancelRequested: boolean
  invalidated: boolean
  cancelReason: ChatRequestCancelReason | null
}

export class ChatRequestController {
  private nextId = 0
  private activeToken: InternalToken | null = null
  private disposed = false

  begin(mode: ChatRequestMode): ChatRequestToken | null {
    if (this.disposed || this.activeToken !== null) return null

    const token: InternalToken = {
      id: ++this.nextId,
      mode,
      cancel: null,
      cancelRequested: false,
      invalidated: false,
      cancelReason: null
    }
    this.activeToken = token
    return token
  }

  isOwner(token: ChatRequestToken | null | undefined): boolean {
    return !!token && this.activeToken === token
  }

  /** 取消或上下文失效后，旧请求可能仍占用活动槽位，但不能继续更新 UI。 */
  canUpdate(token: ChatRequestToken | null | undefined): boolean {
    return this.isOwner(token) && !(token as InternalToken).invalidated
  }

  /** 只允许当前 owner 修改请求模式（Excel not_excel fallback 使用同一生命周期）。 */
  setMode(token: ChatRequestToken, mode: ChatRequestMode): boolean {
    if (!this.canUpdate(token)) return false
    ;(token as InternalToken).mode = mode
    return true
  }

  registerCancel(token: ChatRequestToken, cancel: CancelHandler): boolean {
    if (!this.isOwner(token)) return false
    const current = token as InternalToken
    if (current.cancel !== null) return false

    current.cancel = cancel
    // 取消可能发生在 composable 创建后的 cancel 注册之前。
    if (current.cancelRequested) {
      this.safeCancel(current)
    }
    return true
  }

  /** 用户主动取消：保留本请求更新取消终态的能力，但不释放活动槽位。 */
  cancelActive(reason: ChatRequestCancelReason = 'user'): boolean {
    const current = this.activeToken
    if (!current) return false

    if (reason === 'unmount') current.invalidated = true
    if (current.cancelRequested) return false

    current.cancelRequested = true
    current.cancelReason = reason
    this.safeCancel(current)
    return true
  }

  /** 页面切换/清空/卸载：旧结果不得再写 UI，但活动槽位仍需等底层 Promise finish。 */
  invalidateActive(reason: ChatRequestCancelReason = 'unmount'): boolean {
    const current = this.activeToken
    if (!current) return false

    current.invalidated = true
    if (current.cancelRequested) return false

    current.cancelRequested = true
    current.cancelReason = reason
    this.safeCancel(current)
    return true
  }

  /** 只有当前 owner 能释放页面级活动槽位。 */
  finish(token: ChatRequestToken): boolean {
    if (!this.isOwner(token)) return false
    this.activeToken = null
    return true
  }

  get active(): boolean {
    return this.activeToken !== null
  }

  dispose(): void {
    this.disposed = true
    const current = this.activeToken
    if (!current) return

    current.invalidated = true
    if (current.cancelRequested) return

    current.cancelRequested = true
    current.cancelReason = 'unmount'
    this.safeCancel(current)
  }

  private safeCancel(token: InternalToken): void {
    if (!token.cancel) return
    try {
      token.cancel(token.cancelReason ?? 'unmount')
    } catch (error) {
      // 取消异常不能破坏页面级所有者状态；底层 composable 自己会记录清理异常。
      console.error('取消活动请求失败：', error)
    }
  }
}
