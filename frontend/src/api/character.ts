import { request } from '@/utils/requests'
import type {
  Character,
  CharacterCreate,
  CharacterUpdate,
  SpeakRequest,
  SpeakResponse,
  ConversationRecord,
  ApiResponse
} from "@/types/character.ts";

// 注意：响应拦截器（utils/requests.ts）已把 response.data 解包并返回内层 data，
// 因此 request.get/post/... 的运行时返回值即为内层 data，这里直接断言为业务类型。

// 获取角色列表
export const getCharacters = async (): Promise<Character[]> => {
  const response = await
    request.get<ApiResponse<Character[]>>('/api/v1/characters/')
  return response as unknown as Character[]
}

// 获取单个角色
export const getCharacter = async (id: string): Promise<Character> => {
  const response = await
    request.get<ApiResponse<Character>>(`/api/v1/characters/${id}`)
  return response as unknown as Character
}

// 创建角色
export const createCharacter = async (data: CharacterCreate): Promise<Character> => {
  const response = await
    request.post<ApiResponse<Character>>(`/api/v1/characters/`, data)
  return response as unknown as Character
}

// 更新角色
export const updateCharacter = async (id: string, data: CharacterUpdate): Promise<Character> => {
  const response = await
    request.put<ApiResponse<Character>>(`/api/v1/characters/${id}`, data)
  return response as unknown as Character
}

// 删除角色
export const deleteCharacter = async (id: string): Promise<void> => {
  await request.delete(`/api/v1/characters/${id}`)
}

// 与角色对话
export const speakToCharacter = async (characterId: string, message: SpeakRequest): Promise<SpeakResponse> => {
  const response = await
    request.post<ApiResponse<SpeakResponse>>(`/api/v1/characters/${characterId}/speak`, { message })
  return response as unknown as SpeakResponse
}

// 获取角色对话历史
export const getCharacterConversations = async (
  characterId: string,
  limit: number = 10,
  offset: number = 0
): Promise<{ conversations: ConversationRecord[]; total: number }> => {
  const response = await
    request.get<ApiResponse<{ conversations: ConversationRecord[]; total: number }>>(`/api/v1/characters/${characterId}/conversations`, { params: { limit, offset }}
    )
  return response as unknown as { conversations: ConversationRecord[]; total: number }
}

// 清空角色对话历史（删除对应 Conversation 与全部 Text）
export const clearCharacterConversation = async (characterId: string): Promise<void> => {
  await request.delete(`/api/v1/characters/${characterId}/conversations`)
}

// 获取角色统计信息
export const getCharacterStats = async (characterId: string): Promise<any> => {
  const response = await
    request.get<ApiResponse>(`/api/v1/characters/${characterId}/stats`)
  return response as unknown as any
}
