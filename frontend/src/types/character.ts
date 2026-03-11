export interface Character {
  id: string
  name: string
  system_prompt: string
  model: string
  conversation_count: number
  created_at?: string
  owner_id?: number
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
}

export interface SpeakResponse {
  success: boolean
  response: string
  character_id: string
  timestamp: string
}

export interface ConversationRecord {
  timestamp: string
  user: string
  assistant: string
  model: string
}

export interface ApiResponse<T = any> {
  success: boolean
  code: number
  text: string
  data: T
  timestamp: string
}
