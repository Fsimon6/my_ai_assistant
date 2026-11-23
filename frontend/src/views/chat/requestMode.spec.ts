import { describe, expect, it, vi } from 'vitest'

import { EXCEL_HISTORY_SCHEMA_VERSION, restoreExcelHistory } from './excelHistory'
import {
  canRegenerate,
  canRegenerateMessage,
  historyIdentityFields,
  historyModeFields,
  regenerateTargetIds,
  requestModeFromMetaInfo,
} from './requestMode'

describe('request mode history mapping', () => {
  it('maps chat metadata to chat mode without answer-level fields', () => {
    expect(requestModeFromMetaInfo({ source: 'chat' })).toBe('chat')
    expect(historyModeFields({ source: 'chat' })).toEqual({
      requestMode: 'chat',
      isRAG: false,
    })
    expect(historyModeFields({ source: 'chat' })).not.toHaveProperty('fromKnowledgeBase')
    expect(historyModeFields({ source: 'chat' })).not.toHaveProperty('sources')
  })

  it('maps rag metadata to rag mode without fabricating sources', () => {
    const fields = historyModeFields({ source: 'rag' })

    expect(fields).toEqual({
      requestMode: 'rag',
      isRAG: true,
    })
    expect(fields).not.toHaveProperty('sources')
  })

  it('maps excel metadata to excel mode without answer-level fields', () => {
    const fields = historyModeFields({ source: 'excel', excel: {} })

    expect(fields).toEqual({
      requestMode: 'excel',
      isRAG: false,
    })
    expect(fields).not.toHaveProperty('fromKnowledgeBase')
    expect(fields).not.toHaveProperty('sources')
  })

  it('does not default missing or null metadata to chat', () => {
    expect(requestModeFromMetaInfo(undefined)).toBe('unknown')
    expect(requestModeFromMetaInfo(null)).toBe('unknown')
    expect(historyModeFields(undefined)).toEqual({
      requestMode: 'unknown',
      isRAG: false,
    })
  })

  it('maps empty string and unknown sources to unknown', () => {
    expect(requestModeFromMetaInfo({ source: '' })).toBe('unknown')
    expect(requestModeFromMetaInfo({ source: 'other' })).toBe('unknown')
    expect(requestModeFromMetaInfo({ source: 'RAG' })).toBe('unknown')
  })

  it('maps missing source and non-object metadata to unknown', () => {
    expect(requestModeFromMetaInfo({})).toBe('unknown')
    expect(requestModeFromMetaInfo('rag')).toBe('unknown')
    expect(requestModeFromMetaInfo('')).toBe('unknown')
    expect(requestModeFromMetaInfo([])).toBe('unknown')
  })

  it('restores RAG user mode without answer-level fromKnowledgeBase or sources', () => {
    const user = { role: 'user', meta_info: { source: 'rag' } }
    const fields = historyModeFields(user.meta_info)

    expect(fields).toEqual({
      requestMode: 'rag',
      isRAG: true,
    })
    expect(fields).not.toHaveProperty('fromKnowledgeBase')
    expect(fields).not.toHaveProperty('sources')
  })

  it('restores RAG assistant mode without answer-level fromKnowledgeBase or sources', () => {
    const assistant = { role: 'assistant', meta_info: { source: 'rag' } }
    const fields = historyModeFields(assistant.meta_info)

    expect(fields).toEqual({
      requestMode: 'rag',
      isRAG: true,
    })
    expect(fields).not.toHaveProperty('fromKnowledgeBase')
    expect(fields).not.toHaveProperty('sources')
  })

  it('allows chat and rag regenerate but denies excel and unknown', () => {
    expect(canRegenerate('chat')).toBe(true)
    expect(canRegenerate('rag')).toBe(true)
    expect(canRegenerate('excel')).toBe(false)
    expect(canRegenerate('unknown')).toBe(false)
    expect(canRegenerate(undefined)).toBe(false)
    expect(canRegenerate(null)).toBe(false)
  })

  it('keeps missing, empty, invalid, and null metadata non-regenerable', () => {
    for (const metaInfo of [undefined, null, {}, { source: '' }, { source: 'other' }, 'rag']) {
      expect(canRegenerate(requestModeFromMetaInfo(metaInfo))).toBe(false)
    }
  })

  it('requires database identities for chat and rag regenerate', () => {
    const user = { role: 'user', messageId: 11, requestMode: 'chat' as const }
    expect(canRegenerateMessage({ role: 'assistant', messageId: 12, requestMode: 'chat' }, user)).toBe(true)
    expect(canRegenerateMessage({ role: 'assistant', messageId: 12, requestMode: 'rag' }, user)).toBe(true)

    expect(canRegenerateMessage({ role: 'assistant', requestMode: 'chat' }, user)).toBe(false)
    expect(canRegenerateMessage({ role: 'assistant', messageId: 12, requestMode: 'chat' }, { role: 'user' })).toBe(false)
    expect(canRegenerateMessage({ role: 'assistant', messageId: 12, requestMode: 'excel' }, user)).toBe(false)
    expect(canRegenerateMessage({ role: 'assistant', messageId: 12, requestMode: 'unknown' }, user)).toBe(false)
    expect(canRegenerateMessage({ role: 'user', messageId: 12, requestMode: 'chat' }, user)).toBe(false)
  })

  it('builds regenerate payload only from database message IDs', () => {
    const target = { role: 'assistant', messageId: 22, requestMode: 'rag' as const }
    const user = { role: 'user', messageId: 21 }
    expect(regenerateTargetIds(target, user)).toEqual({
      target_user_message_id: 21,
      target_assistant_message_id: 22,
    })
    expect(regenerateTargetIds({ ...target, messageId: undefined }, user)).toBeNull()
    expect(regenerateTargetIds(target, { role: 'user' })).toBeNull()
  })

  it('maps only integer database IDs into history identity fields', () => {
    expect(historyIdentityFields(10, 3)).toEqual({ messageId: 10, conversationId: 3 })
    expect(historyIdentityFields(undefined, null)).toEqual({})
    expect(historyIdentityFields('10', 3)).toEqual({ conversationId: 3 })
  })

  it('does not regress Excel history restoration', () => {
    const buildExcelTable = vi.fn()
    const buildExcelAgg = vi.fn(() => ({ marker: 'aggregate' }))
    const buildGroupAgg = vi.fn()
    const buildMultiStep = vi.fn()

    const restored = restoreExcelHistory(
      {
        source: 'excel',
        excel: {
          schema_version: EXCEL_HISTORY_SCHEMA_VERSION,
          kind: 'aggregate',
          payload: {},
        },
      },
      { buildExcelTable, buildExcelAgg, buildGroupAgg, buildMultiStep } as any,
    )

    expect(restored.isExcelQuery).toBe(true)
    expect(restored.excelAgg).toEqual({ marker: 'aggregate' })
    expect(buildExcelAgg).toHaveBeenCalledTimes(1)
  })
})
