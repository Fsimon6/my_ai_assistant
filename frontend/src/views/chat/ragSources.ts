import type { RagSource } from '@/types/rag'

/**
 * 将流式 complete 帧中的 sources 收敛为可展示列表。
 * 仅做防御性规范化与去重；不推断不存在的 document_id。
 */
export function normalizeRagSources(raw: unknown): RagSource[] {
  if (!Array.isArray(raw)) return []

  const seen = new Set<string>()
  const result: RagSource[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const value = item as Record<string, unknown>
    const documentId = typeof value.document_id === 'string' ? value.document_id.trim() : ''
    if (!documentId) continue

    const chunkIndex = typeof value.chunk_index === 'number' && Number.isFinite(value.chunk_index)
      ? value.chunk_index
      : null
    const key = `${documentId}::${chunkIndex ?? ''}`
    if (seen.has(key)) continue
    seen.add(key)

    result.push({
      document_id: documentId,
      filename: typeof value.filename === 'string' && value.filename.trim()
        ? value.filename.trim()
        : null,
      chunk_index: chunkIndex,
      score: typeof value.score === 'number' && Number.isFinite(value.score) ? value.score : null,
      snippet: typeof value.snippet === 'string' ? value.snippet : ''
    })
  }
  return result
}

export function ragSourceLabel(source: RagSource): string {
  return source.filename || '未知文档'
}
