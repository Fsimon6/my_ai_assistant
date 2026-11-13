import { describe, expect, it } from 'vitest'

import { buildRagHistory, isRagHistoryMessage } from './ragHistory'

describe('RAG history filtering', () => {
  it('keeps completed turns and legacy messages without status', () => {
    const messages = [
      { role: 'user', content: '旧问题' },
      { role: 'assistant', content: '旧回答' },
      { role: 'user', content: '新问题', turnStatus: 'completed' },
      { role: 'assistant', content: '新回答', streamStatus: 'completed', turnStatus: 'completed' },
    ]

    expect(buildRagHistory(messages)).toEqual([
      { role: 'user', content: '旧问题' },
      { role: 'assistant', content: '旧回答' },
      { role: 'user', content: '新问题' },
      { role: 'assistant', content: '新回答' },
    ])
  })

  it('drops both sides of a failed/interrupted/cancelled/unknown turn', () => {
    const messages = [
      { role: 'user', content: '失败问题', turnStatus: 'failed' },
      { role: 'assistant', content: '错误提示', error: true, streamStatus: 'failed', turnStatus: 'failed' },
      { role: 'user', content: '中断问题', turnStatus: 'interrupted' },
      { role: 'assistant', content: '部分回答', streamStatus: 'interrupted', turnStatus: 'interrupted' },
      { role: 'user', content: '取消问题', turnStatus: 'cancelled' },
      { role: 'assistant', content: '部分回答', streamStatus: 'cancelled', turnStatus: 'cancelled' },
      { role: 'user', content: '正常问题', turnStatus: 'completed' },
      { role: 'assistant', content: '正常回答', streamStatus: 'completed', turnStatus: 'completed' },
    ]

    expect(buildRagHistory(messages)).toEqual([
      { role: 'user', content: '正常问题' },
      { role: 'assistant', content: '正常回答' },
    ])
  })

  it('excludes Excel, streaming, empty/whitespace content and error messages', () => {
    expect(isRagHistoryMessage({ role: 'assistant', content: 'x', isExcelQuery: true })).toBe(false)
    expect(isRagHistoryMessage({ role: 'assistant', content: 'x', isStreaming: true })).toBe(false)
    expect(isRagHistoryMessage({ role: 'assistant', content: '' })).toBe(false)
    expect(isRagHistoryMessage({ role: 'assistant', content: '   ' })).toBe(false)
    expect(isRagHistoryMessage({ role: 'assistant', content: 'x', error: true })).toBe(false)
    expect(isRagHistoryMessage({ role: 'user', content: '问题' })).toBe(true)
  })

  it('clamps negative, NaN and out-of-range endIndex without changing valid semantics', () => {
    const messages = [
      { role: 'user', content: '一' },
      { role: 'assistant', content: '二' },
    ]

    expect(buildRagHistory(messages, -1)).toEqual([])
    expect(buildRagHistory(messages, Number.NaN)).toEqual([])
    expect(buildRagHistory(messages, 999)).toEqual([
      { role: 'user', content: '一' },
      { role: 'assistant', content: '二' },
    ])
    expect(buildRagHistory(messages, 1)).toEqual([{ role: 'user', content: '一' }])
  })

  it('respects the end index and does not include the current user/placeholder', () => {
    const messages = [
      { role: 'user', content: '历史问题', turnStatus: 'completed' },
      { role: 'assistant', content: '历史回答', streamStatus: 'completed', turnStatus: 'completed' },
      { role: 'user', content: '当前问题', turnStatus: 'streaming' },
      { role: 'assistant', content: '', isStreaming: true, streamStatus: 'streaming', turnStatus: 'streaming' },
    ]

    expect(buildRagHistory(messages, 2)).toEqual([
      { role: 'user', content: '历史问题' },
      { role: 'assistant', content: '历史回答' },
    ])
  })
})
