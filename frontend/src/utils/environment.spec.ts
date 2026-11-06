import { describe, expect, it } from 'vitest'
import { isL1TestingEnv, normalizeAppEnv } from './environment'

describe('L1 environment marker', () => {
  it('recognizes testing only when explicitly set', () => {
    expect(isL1TestingEnv('testing')).toBe(true)
    expect(isL1TestingEnv(' testing ')).toBe(true)
    expect(isL1TestingEnv('development')).toBe(false)
    expect(isL1TestingEnv(undefined)).toBe(false)
    expect(isL1TestingEnv('production')).toBe(false)
  })

  it('does not infer testing from unknown values', () => {
    expect(normalizeAppEnv('l1')).toBe('unknown')
    expect(isL1TestingEnv('l1')).toBe(false)
  })
})
