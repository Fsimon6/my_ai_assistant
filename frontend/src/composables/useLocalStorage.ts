import { ref, watch } from "vue"

export function useLocalStorage<T>(key: string, defaultValue: T) {
  const storedValue = localStorage.getItem(key)
  const value = ref<T>(
    storedValue ? JSON.parse(storedValue) : defaultValue
  )

  // 监听变化并保存到localStorage
  watch(value, (newValue) => {
    if (newValue === null || newValue === undefined) {
      localStorage.removeItem(key)
    } else {
      localStorage.setItem(key, JSON.stringify(newValue))
    }
  }, {deep: true})

  const clear = () => {
    value.value = defaultValue
    localStorage.removeItem(key)
  }

  const update = (updater: (current: T) => T) => {
    value.value = updater(value.value)
  }

  return {
    value,
    clear,
    update
  }
}
