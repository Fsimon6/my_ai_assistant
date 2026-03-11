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
import type {LoginRequest, LoginResponse} from "@/types/auth.ts";

// 获取角色列表
export const getCharacters = async (): Promise<Character[]> => {
  const response = await
    request.get<ApiResponse<Character[]>>('/api/v1/characters/')
    return response.data.data || response
}

// 获取单个角色
export const getCharacter = async (id: string): Promise<Character> => {
  const response = await
    request.get<ApiResponse<Character>>(`/api/v1/characters/${id}`)
  return response.data.data || response
}

// 创建角色
export const createCharacter = async (data: CharacterCreate): Promise<Character> => {
  const response = await
    request.post<ApiResponse<Character>>(`/api/v1/characters/`, data)
  return response.data.data || response
}

// 更新角色
export const updateCharacter = async (id: string, data: CharacterUpdate): Promise<Character> => {
  const response = await
    request.put<ApiResponse<Character>>(`/api/v1/characters/${id}`, data)
  return response.data.data || response
}

// 删除角色
export const deleteCharacter = async (id: string): Promise<void> => {
  await request.delete(`/api/v1/characters/${id}`)
}

// 与角色对话
export const speakToCharacter = async (characterId: string, message: SpeakRequest): Promise<SpeakResponse> => {
  const response = await
    request.post<ApiResponse<SpeakResponse>>(`/api/v1/characters/${characterId}/speak`, { message })
  return response.data.data || response
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
  return response.data.data || response
}

// 获取角色统计信息
export const getCharacterStats = async (characterId: string): Promise<any> => {
  const response = await
    request.get<ApiResponse>(`/api/v1/characters/${characterId}/stats`)
  return response.data.data || response
}
