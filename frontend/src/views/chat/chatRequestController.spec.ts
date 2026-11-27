import { describe, expect, it, vi } from 'vitest'

import { ChatRequestController } from './chatRequestController'

describe('ChatRequestController', () => {
  it('B01 allows one request from idle', () => {
    const controller = new ChatRequestController()
    const token = controller.begin('chat')

    expect(token).not.toBeNull()
    expect(controller.active).toBe(true)
    expect(controller.isOwner(token)).toBe(true)
  })

  it('B02 rejects a second request while one is active', () => {
    const controller = new ChatRequestController()
    const first = controller.begin('chat')!

    expect(controller.begin('rag')).toBeNull()
    expect(controller.isOwner(first)).toBe(true)
    expect(controller.active).toBe(true)
  })

  it('B03 finish releases the active request', () => {
    const controller = new ChatRequestController()
    const token = controller.begin('chat')!

    expect(controller.finish(token)).toBe(true)
    expect(controller.active).toBe(false)
    expect(controller.isOwner(token)).toBe(false)
  })

  it('B04 allows a new request after finish', () => {
    const controller = new ChatRequestController()
    const first = controller.begin('chat')!
    controller.finish(first)

    const second = controller.begin('rag')

    expect(second).not.toBeNull()
    expect(controller.isOwner(second)).toBe(true)
    expect(controller.isOwner(first)).toBe(false)
  })

  it('B05 old token cannot finish a new request', () => {
    const controller = new ChatRequestController()
    const first = controller.begin('chat')!
    controller.finish(first)
    const second = controller.begin('rag')!

    expect(controller.finish(first)).toBe(false)
    expect(controller.isOwner(second)).toBe(true)
    expect(controller.active).toBe(true)
  })

  it('B06 old token cannot register cancel on a new request', () => {
    const controller = new ChatRequestController()
    const first = controller.begin('chat')!
    controller.finish(first)
    const second = controller.begin('rag')!
    const firstCancel = vi.fn()
    const secondCancel = vi.fn()

    expect(controller.registerCancel(first, firstCancel)).toBe(false)
    expect(controller.registerCancel(second, secondCancel)).toBe(true)
    expect(firstCancel).not.toHaveBeenCalled()
    expect(secondCancel).not.toHaveBeenCalled()
  })

  it('B07 cancel only affects the owning request', () => {
    const controller = new ChatRequestController()
    const first = controller.begin('chat')!
    const firstCancel = vi.fn()
    controller.registerCancel(first, firstCancel)

    expect(controller.cancelActive('user')).toBe(true)
    expect(firstCancel).toHaveBeenCalledTimes(1)
    expect(controller.isOwner(first)).toBe(true)
    expect(controller.begin('rag')).toBeNull()
    expect(controller.canUpdate(first)).toBe(true)
  })

  it('B08 repeated cancel is idempotent', () => {
    const controller = new ChatRequestController()
    const token = controller.begin('chat')!
    const cancel = vi.fn()
    controller.registerCancel(token, cancel)

    expect(controller.cancelActive('user')).toBe(true)
    expect(controller.cancelActive('user')).toBe(false)
    expect(cancel).toHaveBeenCalledTimes(1)
  })

  it('B09 cancel before cancel registration invokes the later handler', () => {
    const controller = new ChatRequestController()
    const token = controller.begin('chat')!
    const cancel = vi.fn()

    expect(controller.cancelActive('user')).toBe(true)
    expect(controller.registerCancel(token, cancel)).toBe(true)
    expect(cancel).toHaveBeenCalledTimes(1)
    expect(cancel).toHaveBeenCalledWith('user')
  })

  it('B10 cancel registration after finish cannot revive the request', () => {
    const controller = new ChatRequestController()
    const token = controller.begin('chat')!
    controller.finish(token)
    const cancel = vi.fn()

    expect(controller.registerCancel(token, cancel)).toBe(false)
    expect(cancel).not.toHaveBeenCalled()
    expect(controller.active).toBe(false)
  })

  it('B11 invalidated request cannot update UI', () => {
    const controller = new ChatRequestController()
    const token = controller.begin('rag')!

    expect(controller.canUpdate(token)).toBe(true)
    expect(controller.invalidateActive('unmount')).toBe(true)
    expect(controller.canUpdate(token)).toBe(false)
    expect(controller.isOwner(token)).toBe(true)
  })

  it('B12 old token cannot clean up a new token', () => {
    const controller = new ChatRequestController()
    const first = controller.begin('chat')!
    controller.finish(first)
    const second = controller.begin('rag')!

    expect(controller.finish(first)).toBe(false)
    expect(controller.canUpdate(first)).toBe(false)
    expect(controller.canUpdate(second)).toBe(true)
    expect(controller.active).toBe(true)
  })

  it('B13 dispose rejects new requests and cancels the active one', () => {
    const controller = new ChatRequestController()
    const token = controller.begin('excel')!
    const cancel = vi.fn()
    controller.registerCancel(token, cancel)

    controller.dispose()

    expect(cancel).toHaveBeenCalledWith('unmount')
    expect(controller.begin('chat')).toBeNull()
    expect(controller.canUpdate(token)).toBe(false)
    expect(controller.finish(token)).toBe(true)
    expect(controller.active).toBe(false)
  })

  it('B14 two controller instances are isolated', () => {
    const first = new ChatRequestController()
    const second = new ChatRequestController()
    const tokenA = first.begin('chat')!
    const tokenB = second.begin('rag')!

    expect(first.isOwner(tokenA)).toBe(true)
    expect(second.isOwner(tokenB)).toBe(true)
    expect(first.isOwner(tokenB)).toBe(false)
    expect(second.isOwner(tokenA)).toBe(false)
    expect(first.finish(tokenA)).toBe(true)
    expect(second.active).toBe(true)
  })

  it('B15 finish after an early failure allows a new request', () => {
    const controller = new ChatRequestController()
    const failed = controller.begin('chat')!

    // 模拟同步启动失败后由调用方 finally 释放。
    expect(controller.finish(failed)).toBe(true)
    const retry = controller.begin('chat')

    expect(retry).not.toBeNull()
    expect(controller.isOwner(retry)).toBe(true)
  })
})
