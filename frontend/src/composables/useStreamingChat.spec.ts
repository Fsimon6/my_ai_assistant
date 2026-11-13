import { afterEach, describe, expect, it, vi } from 'vitest'

import { useStreamingChat } from './useStreamingChat'

vi.mock('@/utils/requests', () => ({
  request: { defaults: { baseURL: '' } }
}))

function ndjsonResponse(lines: string[]): Response {
  return new Response(lines.join('\n') + '\n', {
    status: 200,
    headers: { 'Content-Type': 'application/x-ndjson' }
  })
}

function rawResponse(body: string): Response {
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'application/x-ndjson' }
  })
}

function abortError(): Error {
  const error = new Error('aborted')
  error.name = 'AbortError'
  return error
}

describe('useStreamingChat terminal states', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  function installStorage() {
    vi.stubGlobal('localStorage', { getItem: () => null })
  }

  it('S01/S03 passes sources from complete frame to onComplete', async () => {
    installStorage()
    const source = {
      document_id: 'doc-a',
      filename: 'a.pdf',
      chunk_index: 1,
      score: 0.8,
      snippet: 'alpha'
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ndjsonResponse([
      JSON.stringify({ type: 'chunk', content: 'answer' }),
      JSON.stringify({ type: 'complete', content: 'answer', fullResponse: 'answer', sources: [source] })
    ])))

    const onComplete = vi.fn()
    const { sendMessage } = useStreamingChat({ characterId: '1', onComplete })
    await sendMessage('问题')

    expect(onComplete).toHaveBeenCalledTimes(1)
    expect(onComplete).toHaveBeenCalledWith('answer', [source])
  })

  it('S02 uses empty sources when complete has no sources', async () => {
    installStorage()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ndjsonResponse([
      JSON.stringify({ type: 'complete', content: 'answer', fullResponse: 'answer' })
    ])))

    const onComplete = vi.fn()
    const { sendMessage } = useStreamingChat({ characterId: '1', onComplete })
    await sendMessage('问题')

    expect(onComplete).toHaveBeenCalledWith('answer', [])
  })

  it('S04 treats EOF without complete as INTERRUPTED, not success', async () => {
    installStorage()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ndjsonResponse([
      JSON.stringify({ type: 'chunk', content: 'partial' })
    ])))

    const onComplete = vi.fn()
    const onInterrupted = vi.fn()
    const { sendMessage } = useStreamingChat({ characterId: '1', onComplete, onInterrupted })
    await sendMessage('问题')

    expect(onComplete).not.toHaveBeenCalled()
    expect(onInterrupted).toHaveBeenCalledWith('partial', 'eof')
  })

  it('S03 treats error as FAILED and never complete', async () => {
    installStorage()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ndjsonResponse([
      JSON.stringify({ type: 'chunk', content: 'partial' }),
      JSON.stringify({ type: 'error', error: 'provider down' })
    ])))

    const onComplete = vi.fn()
    const onError = vi.fn()
    const { sendMessage } = useStreamingChat({ characterId: '1', onComplete, onError })
    await sendMessage('问题')

    expect(onComplete).not.toHaveBeenCalled()
    expect(onError).toHaveBeenCalledTimes(1)
  })

  it('S06 aborts as CANCELLED, not onError', async () => {
    installStorage()
    vi.stubGlobal('fetch', vi.fn((_url: string, init: RequestInit) => new Promise((_resolve, reject) => {
      init.signal?.addEventListener('abort', () => reject(abortError()))
    })))

    const onCancelled = vi.fn()
    const onError = vi.fn()
    const { sendMessage, cancel } = useStreamingChat({ characterId: '1', onCancelled, onError })
    const pending = sendMessage('问题')
    cancel()
    await pending

    expect(onCancelled).toHaveBeenCalledTimes(1)
    expect(onError).not.toHaveBeenCalled()
  })

  it('S18 unmount cancel is silent', async () => {
    installStorage()
    vi.stubGlobal('fetch', vi.fn((_url: string, init: RequestInit) => new Promise((_resolve, reject) => {
      init.signal?.addEventListener('abort', () => reject(abortError()))
    })))

    const onCancelled = vi.fn()
    const { sendMessage, cancel } = useStreamingChat({ characterId: '1', onCancelled })
    const pending = sendMessage('问题')
    cancel('unmount')
    await pending

    expect(onCancelled).not.toHaveBeenCalled()
  })

  it('S07 malformed JSON becomes UNKNOWN and cannot be overwritten by complete', async () => {
    installStorage()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(rawResponse([
      '{"type":"chunk","content":"partial"}',
      '{not-json}',
      JSON.stringify({ type: 'complete', content: 'answer', fullResponse: 'answer' })
    ].join('\n'))))

    const onComplete = vi.fn()
    const onInterrupted = vi.fn()
    const { sendMessage } = useStreamingChat({ characterId: '1', onComplete, onInterrupted })
    await sendMessage('问题')

    expect(onComplete).not.toHaveBeenCalled()
    expect(onInterrupted).toHaveBeenCalledTimes(1)
    expect(onInterrupted).toHaveBeenCalledWith('partial', 'protocol')
  })

  it('S08 malformed complete is UNKNOWN', async () => {
    installStorage()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(rawResponse('{"type":"complete"')))

    const onComplete = vi.fn()
    const onInterrupted = vi.fn()
    const { sendMessage } = useStreamingChat({ characterId: '1', onComplete, onInterrupted })
    await sendMessage('问题')

    expect(onComplete).not.toHaveBeenCalled()
    expect(onInterrupted).toHaveBeenCalledWith('', 'protocol')
  })

  it('S09 duplicate complete only fires once', async () => {
    installStorage()
    const complete = JSON.stringify({ type: 'complete', content: 'answer', fullResponse: 'answer' })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ndjsonResponse([complete, complete])))

    const onComplete = vi.fn()
    const { sendMessage } = useStreamingChat({ characterId: '1', onComplete })
    await sendMessage('问题')

    expect(onComplete).toHaveBeenCalledTimes(1)
  })

  it('S10 complete then abort keeps COMPLETED', async () => {
    installStorage()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ndjsonResponse([
      JSON.stringify({ type: 'complete', content: 'answer', fullResponse: 'answer' })
    ])))

    const onComplete = vi.fn()
    const onCancelled = vi.fn()
    const { sendMessage, cancel } = useStreamingChat({ characterId: '1', onComplete, onCancelled })
    await sendMessage('问题')
    cancel()

    expect(onComplete).toHaveBeenCalledTimes(1)
    expect(onCancelled).not.toHaveBeenCalled()
  })

  it('S11 error then complete keeps FAILED', async () => {
    installStorage()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ndjsonResponse([
      JSON.stringify({ type: 'error', error: 'provider down' }),
      JSON.stringify({ type: 'complete', content: 'answer', fullResponse: 'answer' })
    ])))

    const onComplete = vi.fn()
    const onError = vi.fn()
    const { sendMessage } = useStreamingChat({ characterId: '1', onComplete, onError })
    await sendMessage('问题')

    expect(onError).toHaveBeenCalledTimes(1)
    expect(onComplete).not.toHaveBeenCalled()
  })

  it('S12 ignores unknown frames but accepts a later complete', async () => {
    installStorage()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ndjsonResponse([
      JSON.stringify({ type: 'heartbeat' }),
      JSON.stringify({ type: 'complete', content: 'answer', fullResponse: 'answer' })
    ])))

    const onComplete = vi.fn()
    const { sendMessage } = useStreamingChat({ characterId: '1', onComplete })
    await sendMessage('问题')

    expect(onComplete).toHaveBeenCalledWith('answer', [])
  })

  it('S13 accepts a complete JSON without final newline', async () => {
    installStorage()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(rawResponse(
      JSON.stringify({ type: 'complete', content: 'answer', fullResponse: 'answer' })
    )))

    const onComplete = vi.fn()
    const onInterrupted = vi.fn()
    const { sendMessage } = useStreamingChat({ characterId: '1', onComplete, onInterrupted })
    await sendMessage('问题')

    expect(onComplete).toHaveBeenCalledWith('answer', [])
    expect(onInterrupted).not.toHaveBeenCalled()
  })

  it('S14 truncated final JSON is not success', async () => {
    installStorage()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(rawResponse(
      '{"type":"complete","content":"answer"'
    )))

    const onComplete = vi.fn()
    const onInterrupted = vi.fn()
    const { sendMessage } = useStreamingChat({ characterId: '1', onComplete, onInterrupted })
    await sendMessage('问题')

    expect(onComplete).not.toHaveBeenCalled()
    expect(onInterrupted).toHaveBeenCalledWith('', 'protocol')
  })

  it('S05 network read failure is INTERRUPTED', async () => {
    installStorage()
    let reads = 0
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (reads++ === 0) {
          controller.enqueue(new TextEncoder().encode(
            JSON.stringify({ type: 'chunk', content: 'partial' }) + '\n'
          ))
        } else {
          controller.error(new Error('network down'))
        }
      }
    })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(stream, { status: 200 })))

    const onInterrupted = vi.fn()
    const { sendMessage } = useStreamingChat({ characterId: '1', onInterrupted })
    await sendMessage('问题')

    expect(onInterrupted).toHaveBeenCalledWith('partial', 'network')
  })

  it('isolates callback exceptions from protocol errors', async () => {
    installStorage()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ndjsonResponse([
      JSON.stringify({ type: 'complete', content: 'answer', fullResponse: 'answer' })
    ])))
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    const onError = vi.fn()
    const onInterrupted = vi.fn()
    const { sendMessage } = useStreamingChat({
      characterId: '1',
      onComplete: () => { throw new Error('callback boom') },
      onError,
      onInterrupted,
    })
    await sendMessage('问题')

    expect(onError).not.toHaveBeenCalled()
    expect(onInterrupted).not.toHaveBeenCalled()
    expect(consoleError).toHaveBeenCalled()
  })

  it('cancels and releases the reader when complete terminates early', async () => {
    installStorage()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ndjsonResponse([
      JSON.stringify({ type: 'chunk', content: 'answer' }),
      JSON.stringify({ type: 'complete', content: 'answer', fullResponse: 'answer' })
    ])))
    const cancel = vi.spyOn(ReadableStreamDefaultReader.prototype, 'cancel')
    const release = vi.spyOn(ReadableStreamDefaultReader.prototype, 'releaseLock')

    const { sendMessage } = useStreamingChat({ characterId: '1' })
    await sendMessage('问题')

    expect(cancel).toHaveBeenCalled()
    expect(release).toHaveBeenCalled()
  })

  it('does not cancel a naturally completed reader', async () => {
    installStorage()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(rawResponse(
      JSON.stringify({ type: 'complete', content: 'answer', fullResponse: 'answer' })
    )))
    const cancel = vi.spyOn(ReadableStreamDefaultReader.prototype, 'cancel')
    const release = vi.spyOn(ReadableStreamDefaultReader.prototype, 'releaseLock')

    const { sendMessage } = useStreamingChat({ characterId: '1' })
    await sendMessage('问题')

    expect(cancel).not.toHaveBeenCalled()
    expect(release).toHaveBeenCalled()
  })

  it('cancels the reader on error and protocol termination', async () => {
    installStorage()
    const cancel = vi.spyOn(ReadableStreamDefaultReader.prototype, 'cancel')
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ndjsonResponse([
      JSON.stringify({ type: 'chunk', content: 'partial' }),
      JSON.stringify({ type: 'error', error: 'provider down' })
    ])))
    const first = useStreamingChat({ characterId: '1' })
    await first.sendMessage('问题')
    expect(cancel).toHaveBeenCalled()

    cancel.mockClear()
    let reads = 0
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (reads++ === 0) {
          controller.enqueue(new TextEncoder().encode(
            JSON.stringify({ type: 'chunk', content: 'partial' }) + '\n{bad-json}\n'
          ))
        }
      }
    })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(stream, { status: 200 })))
    const second = useStreamingChat({ characterId: '1' })
    await second.sendMessage('问题')
    expect(cancel).toHaveBeenCalled()
  })

  it('does not let reader.cancel rejection override COMPLETED', async () => {
    installStorage()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ndjsonResponse([
      JSON.stringify({ type: 'complete', content: 'answer', fullResponse: 'answer' })
    ])))
    const cancel = vi.spyOn(ReadableStreamDefaultReader.prototype, 'cancel')
      .mockRejectedValue(new Error('cancel failed'))
    const release = vi.spyOn(ReadableStreamDefaultReader.prototype, 'releaseLock')
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    const onComplete = vi.fn()

    const { sendMessage } = useStreamingChat({ characterId: '1', onComplete })
    await sendMessage('问题')

    expect(onComplete).toHaveBeenCalledTimes(1)
    expect(cancel).toHaveBeenCalled()
    expect(release).toHaveBeenCalled()
    expect(consoleError).toHaveBeenCalled()
  })

  it('isolates onChunk and onError callback exceptions', async () => {
    installStorage()
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    const onChunk = vi.fn(() => { throw new Error('chunk callback') })
    const onError = vi.fn(() => { throw new Error('error callback') })
    const onInterrupted = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ndjsonResponse([
      JSON.stringify({ type: 'chunk', content: 'partial' }),
      JSON.stringify({ type: 'error', error: 'provider down' })
    ])))

    const { sendMessage } = useStreamingChat({ characterId: '1', onChunk, onError, onInterrupted })
    await sendMessage('问题')

    expect(onChunk).toHaveBeenCalledTimes(1)
    expect(onError).toHaveBeenCalledTimes(1)
    expect(onInterrupted).not.toHaveBeenCalled()
    expect(consoleError).toHaveBeenCalled()
  })

  it('isolates onCancelled and onInterrupted callback exceptions', async () => {
    installStorage()
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.stubGlobal('fetch', vi.fn((_url: string, init: RequestInit) => new Promise((_resolve, reject) => {
      init.signal?.addEventListener('abort', () => reject(abortError()))
    })))
    const onCancelled = vi.fn(() => { throw new Error('cancel callback') })
    const { sendMessage, cancel } = useStreamingChat({ characterId: '1', onCancelled })
    const pending = sendMessage('问题')
    cancel('user')
    await pending

    expect(onCancelled).toHaveBeenCalledTimes(1)

    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ndjsonResponse([
      JSON.stringify({ type: 'chunk', content: 'partial' })
    ])))
    const onInterrupted = vi.fn(() => { throw new Error('interrupted callback') })
    const second = useStreamingChat({ characterId: '1', onInterrupted })
    await second.sendMessage('问题')

    expect(onInterrupted).toHaveBeenCalledTimes(1)
    expect(consoleError).toHaveBeenCalled()
  })

  it('handles HTTP failure, fetch failure and empty body', async () => {
    installStorage()
    const onError = vi.fn()
    const onInterrupted = vi.fn()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: 'bad' }), { status: 500 }
    )))
    const http = useStreamingChat({ characterId: '1', onError, onInterrupted })
    await http.sendMessage('问题')
    expect(onError).toHaveBeenCalledTimes(1)
    expect(onInterrupted).not.toHaveBeenCalled()

    onError.mockClear()
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network down')))
    const network = useStreamingChat({ characterId: '1', onError, onInterrupted })
    await network.sendMessage('问题')
    expect(onError).not.toHaveBeenCalled()
    expect(onInterrupted).toHaveBeenCalledWith('', 'network')

    onInterrupted.mockClear()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 200 })))
    const empty = useStreamingChat({ characterId: '1', onError, onInterrupted })
    await empty.sendMessage('问题')
    expect(onInterrupted).toHaveBeenCalledWith('', 'eof')
  })

  it('unknown frame followed by EOF is INTERRUPTED', async () => {
    installStorage()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ndjsonResponse([
      JSON.stringify({ type: 'heartbeat' })
    ])))
    const onInterrupted = vi.fn()
    const onComplete = vi.fn()
    const { sendMessage } = useStreamingChat({ characterId: '1', onInterrupted, onComplete })
    await sendMessage('问题')

    expect(onInterrupted).toHaveBeenCalledWith('', 'eof')
    expect(onComplete).not.toHaveBeenCalled()
  })

  it('generation guard prevents old finally from clearing a new request', async () => {
    installStorage()
    let call = 0
    vi.stubGlobal('fetch', vi.fn((_url: string, init: RequestInit) => {
      call += 1
      return new Promise((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(abortError()))
      })
    }))

    const { sendMessage, cancel, isStreaming } = useStreamingChat({ characterId: '1' })
    const first = sendMessage('first')
    cancel('user')
    const second = sendMessage('second')
    await first

    expect(isStreaming.value).toBe(true)

    cancel('user')
    await second
    expect(isStreaming.value).toBe(false)
    expect(call).toBe(2)
  })
})
