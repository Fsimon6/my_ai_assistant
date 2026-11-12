import { describe, expect, it } from 'vitest'

import { normalizeRagSources, ragSourceLabel } from './ragSources'

describe('normalizeRagSources', () => {
  it('keeps real fields and deduplicates by document_id + chunk_index', () => {
    const sources = normalizeRagSources([
      {
        document_id: 'doc-a',
        filename: 'a.pdf',
        chunk_index: 2,
        score: 0.75,
        snippet: 'alpha'
      },
      {
        document_id: 'doc-a',
        filename: 'a.pdf',
        chunk_index: 2,
        score: 0.5,
        snippet: 'duplicate'
      },
      {
        document_id: 'doc-b',
        chunk_index: 1
      }
    ])

    expect(sources).toEqual([
      {
        document_id: 'doc-a',
        filename: 'a.pdf',
        chunk_index: 2,
        score: 0.75,
        snippet: 'alpha'
      },
      {
        document_id: 'doc-b',
        filename: null,
        chunk_index: 1,
        score: null,
        snippet: ''
      }
    ])
  })

  it('returns empty for missing/invalid payload', () => {
    expect(normalizeRagSources(undefined)).toEqual([])
    expect(normalizeRagSources({})).toEqual([])
    expect(normalizeRagSources([{ filename: 'no-id.pdf' }])).toEqual([])
  })

  it('uses a readable fallback label without fabricating a filename', () => {
    expect(ragSourceLabel({
      document_id: 'doc-a',
      filename: null,
      chunk_index: null,
      score: null,
      snippet: ''
    })).toBe('未知文档')
  })
})
