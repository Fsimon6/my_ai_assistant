import {ref} from 'vue'
import {defineStore} from 'pinia'
import type {Character, CharacterCreate} from '@/types/character'
import * as characterApi from '@/api/character'
// import { request } from '@/utils/requests'

export const useCharacterStore = defineStore('character', () => {
  //状态
  const characters = ref<Character[]>([])
  const currentCharacter = ref<Character | null>(null)
  const isLoading = ref(false)

  // 获取角色列表
  const fetchCharacters = async () => {
    isLoading.value = true
    try {
      characters.value = await characterApi.getCharacters()
    } finally {
      isLoading.value = false
    }
  }

  // 获取单个角色
  const fetchCharacter = async (id: string) => {
    isLoading.value = true
    try {
      currentCharacter.value = await characterApi.getCharacter(id)
    } finally {
      isLoading.value = false
    }
  }

  // 创建角色
  const createCharacter = async (characterData: CharacterCreate) => {
    isLoading.value = true
    try {
      const newCharacter = await characterApi.createCharacter(characterData)

      characters.value.push(newCharacter)
      return newCharacter
    } finally {
      isLoading.value = false
    }
  }

  // 更新角色
  const updateCharacter = async (id: string, characterData: Partial<Character>) => {
    isLoading.value = true
    try {
      const updateCharacter = await characterApi.updateCharacter(id, characterData)

      // 更新列表中的角色
      const index = characters.value.findIndex(c => c.id === id)
      if (index !== -1) {
        characters.value[index] = updateCharacter
      }

      // 如果当前正在查看的角色被更新
      if (currentCharacter.value?.id === id) {
        currentCharacter.value = updateCharacter
      }

      return updateCharacter
    } finally {
      isLoading.value = false
    }
  }

  // 删除角色
  const deleteCharacter = async (id: string) => {
    isLoading.value = true
    try {
      await characterApi.deleteCharacter(id)

      // 从列表中移除
      characters.value = characters.value.filter(c => c.id !== id)

      // 如果当前正在查看的角色被删除
      if (currentCharacter.value?.id === id)
        currentCharacter.value = null
    } finally {
      isLoading.value = false
    }
  }

  // 与角色对话
  const speakToCharacter = async (characterId: string, message: string) => {
    try {
      // 改为传递对象
      return await characterApi.speakToCharacter(characterId, { message: message })
    } catch (error) {
      console.error('对话失败', error)
      throw error
    }
  }

  return {
    characters,
    currentCharacter,
    isLoading,
    fetchCharacters,
    fetchCharacter,
    createCharacter,
    updateCharacter,
    deleteCharacter,
    speakToCharacter
  }
})
