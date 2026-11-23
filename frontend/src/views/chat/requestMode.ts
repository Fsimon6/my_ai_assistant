/**
 * 历史消息请求模式恢复。
 *
 * 只使用后端已有 `meta_info.source`，不根据内容、索引或 UI 状态猜测模式。
 * 旧消息/非法 metadata 一律为 unknown，不默认成 chat 或 RAG。
 */
export type RequestMode = 'chat' | 'rag' | 'excel' | 'unknown'

export interface RequestModeFields {
  requestMode: RequestMode
  isRAG: boolean
}

export function requestModeFromMetaInfo(metaInfo: unknown): RequestMode {
  if (!metaInfo || typeof metaInfo !== 'object' || Array.isArray(metaInfo)) {
    return 'unknown'
  }

  const source = (metaInfo as { source?: unknown }).source
  if (source === 'chat' || source === 'rag' || source === 'excel') {
    return source
  }
  return 'unknown'
}

/** 供历史 mapper 展开到消息对象；不会生成 sources。 */
export function historyModeFields(metaInfo: unknown): RequestModeFields {
  const requestMode = requestModeFromMetaInfo(metaInfo)
  return {
    requestMode,
    isRAG: requestMode === 'rag'
  }
}

/** S17-C：普通聊天与 RAG 均可重新生成；Excel/unknown 保持禁用。 */
export function canRegenerate(requestMode: RequestMode | null | undefined): boolean {
  return requestMode === 'chat' || requestMode === 'rag'
}

export interface RegenerateMessageLike {
  role?: string
  requestMode?: RequestMode | null
  messageId?: unknown
}

/** 仅数据库历史消息具备可替换身份；新生成但未刷新 ID 的消息必须 fail closed。 */
export function canRegenerateMessage(
  target: RegenerateMessageLike | null | undefined,
  targetUser: RegenerateMessageLike | null | undefined,
): boolean {
  return (
    target?.role === 'assistant'
    && targetUser?.role === 'user'
    && canRegenerate(target?.requestMode)
    && Number.isInteger(target?.messageId)
    && Number.isInteger(targetUser?.messageId)
  )
}

export interface RegenerateTargetIds {
  target_user_message_id: number
  target_assistant_message_id: number
}

/** 仅从消息携带的数据库 ID 生成目标字段，禁止使用数组索引/文本匹配。 */
export function regenerateTargetIds(
  target: RegenerateMessageLike | null | undefined,
  targetUser: RegenerateMessageLike | null | undefined,
): RegenerateTargetIds | null {
  if (!canRegenerateMessage(target, targetUser)) return null
  return {
    target_user_message_id: targetUser!.messageId as number,
    target_assistant_message_id: target!.messageId as number,
  }
}

/** 历史 mapper 只保留真实数据库身份，不生成临时 ID。 */
export function historyIdentityFields(id: unknown, conversationId: unknown) {
  return {
    ...(Number.isInteger(id) ? { messageId: id as number } : {}),
    ...(Number.isInteger(conversationId) ? { conversationId: conversationId as number } : {}),
  }
}
