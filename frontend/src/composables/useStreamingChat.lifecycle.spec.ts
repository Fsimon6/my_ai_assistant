import { afterEach, describe, expect, it, vi } from 'vitest'
import { createRenderer, defineComponent, h } from '@vue/runtime-core'

import { useStreamingChat } from './useStreamingChat'

vi.mock('@/utils/requests', () => ({
  request: { defaults: { baseURL: '' } }
}))

function createTestRenderer() {
  return createRenderer({
    patchProp() {},
    insert() {},
    remove() {},
    createElement() { return {} },
    createText() { return {} },
    createComment() { return {} },
    setText() {},
    setElementText() {},
    parentNode() { return null },
    nextSibling() { return null },
    setScopeId() {},
    cloneNode() { return {} },
    insertStaticContent() { return [{}, {}] }
  } as any)
}

function abortError(): Error {
  const error = new Error('aborted')
  error.name = 'AbortError'
  return error
}

describe('useStreamingChat Vue lifecycle', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('registers onUnmounted in setup and silently aborts the active request', async () => {
    vi.stubGlobal('localStorage', { getItem: () => null })
    const onCancelled = vi.fn()
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    let stream: ReturnType<typeof useStreamingChat> | null = null
    let signal: AbortSignal | undefined

    vi.stubGlobal('fetch', vi.fn((_url: string, init: RequestInit) => {
      signal = init.signal || undefined
      return new Promise((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(abortError()))
      })
    }))

    const Comp = defineComponent({
      setup() {
        stream = useStreamingChat({ characterId: '1', onCancelled })
        return () => h('div')
      }
    })

    const renderer = createTestRenderer()
    const app = renderer.createApp(Comp)
    app.mount({} as any)

    const pending = stream!.sendMessage('问题')
    app.unmount()
    await pending

    expect(signal?.aborted).toBe(true)
    expect(onCancelled).not.toHaveBeenCalled()
    expect(warn.mock.calls.some(args => String(args[0]).includes('onUnmounted'))).toBe(false)
  })

  it('does not override a completed request on unmount', async () => {
    vi.stubGlobal('localStorage', { getItem: () => null })
    const onComplete = vi.fn()
    const onCancelled = vi.fn()
    let stream: ReturnType<typeof useStreamingChat> | null = null

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ type: 'complete', content: 'ok', fullResponse: 'ok' }) + '\n',
      { status: 200 }
    )))

    const Comp = defineComponent({
      setup() {
        stream = useStreamingChat({ characterId: '1', onComplete, onCancelled })
        return () => h('div')
      }
    })

    const renderer = createTestRenderer()
    const app = renderer.createApp(Comp)
    app.mount({} as any)

    await stream!.sendMessage('问题')
    app.unmount()

    expect(onComplete).toHaveBeenCalledTimes(1)
    expect(onCancelled).not.toHaveBeenCalled()
  })
})
