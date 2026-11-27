export interface Character {
  id: string
  name: string
  system_prompt: string
  model: string
  conversation_count: number
  created_at?: string
  owner_id?: number
  supportsRAG?: boolean
}

export interface CharacterCreate {
  name: string
  system_prompt: string
  model?: string
  api_key?: string
}

export interface CharacterUpdate {
  name?: string
  system_prompt?: string
  model?: string
  api_key?: string
}

export interface SpeakRequest {
  message: string
  target_user_message_id?: number
  target_assistant_message_id?: number
}

export interface SpeakResponse {
  success: boolean
  response: string
  character_id: string
  timestamp: string
}

export interface ConversationRecord {
  id: number
  conversation_id: number
  role: 'user' | 'assistant'
  content: string
  created_at?: string | null
  meta_info?: Record<string, any> | null
}

export interface ApiResponse<T = any> {
  success: boolean
  code: number
  text: string
  data: T
  timestamp: string
}
