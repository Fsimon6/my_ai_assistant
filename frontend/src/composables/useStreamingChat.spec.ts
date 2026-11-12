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

describe('useStreamingChat RAG sources', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('passes sources from complete frame to onComplete', async () => {
    vi.stubGlobal('localStorage', { getItem: () => null })
    const source = {
      document_id: 'doc-a',
      filename: 'a.pdf',
      chunk_index: 1,
      score: 0.8,
      snippet: 'alpha'
    }
    const fetchMock = vi.fn().mockResolvedValue(ndjsonResponse([
      JSON.stringify({ type: 'chunk', content: 'answer' }),
      JSON.stringify({ type: 'complete', content: 'answer', fullResponse: 'answer', sources: [source] })
    ]))
    vi.stubGlobal('fetch', fetchMock)

    const onComplete = vi.fn()
    const { sendMessage } = useStreamingChat({
      characterId: '1',
      onComplete
    })

    await sendMessage('问题')

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(onComplete).toHaveBeenCalledWith('answer', [source])
  })

  it('uses empty sources when complete has no sources', async () => {
    vi.stubGlobal('localStorage', { getItem: () => null })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ndjsonResponse([
      JSON.stringify({ type: 'complete', content: 'answer', fullResponse: 'answer' })
    ])))

    const onComplete = vi.fn()
    const { sendMessage } = useStreamingChat({ characterId: '1', onComplete })
    await sendMessage('问题')

    expect(onComplete).toHaveBeenCalledWith('answer', [])
  })
})
