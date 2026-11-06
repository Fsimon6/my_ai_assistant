import { describe, expect, it } from 'vitest'
import { buildApiUrl, resolveApiBaseUrl } from './apiBase'

describe('API base isolation', () => {
  it('keeps an explicit L1 origin and normalizes trailing slash', () => {
    expect(resolveApiBaseUrl('http://127.0.0.1:5191/')).toBe('http://127.0.0.1:5191')
  })

  it('uses the page origin when no explicit base is configured', () => {
    expect(resolveApiBaseUrl(undefined)).toBe('')
    expect(resolveApiBaseUrl('')).toBe('')
    expect(resolveApiBaseUrl('   ')).toBe('')
  })

  it('documents the current behavior for a malformed explicit value', () => {
    // The selector intentionally does not silently rewrite caller input.
    // A malformed explicit URL remains malformed instead of falling back to 8021.
    expect(resolveApiBaseUrl('not a url')).toBe('not a url')
  })

  it('builds SSE URLs without duplicating /api or losing it', () => {
    expect(buildApiUrl('http://127.0.0.1:5191', '/api/v1/characters/1/speak/stream'))
      .toBe('http://127.0.0.1:5191/api/v1/characters/1/speak/stream')
    expect(buildApiUrl('', '/api/v1/characters/1/speak/stream'))
      .toBe('/api/v1/characters/1/speak/stream')
    expect(buildApiUrl('http://127.0.0.1:5191/', 'api/v1/characters/1/speak/stream'))
      .toBe('http://127.0.0.1:5191/api/v1/characters/1/speak/stream')
  })
})
