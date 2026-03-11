import type {App} from 'vue'

// 自动聚焦指令
export const vFocus = {
  mounted(el: HTMLElement) {
    el.focus()
  }
}

// 点击外部指令 - 使用 WeakMap 存储处理器
const clickOutsideHandlers = new WeakMap<HTMLElement, (event: MouseEvent) => void>()

export const vClickOutside = {
  beforeMount(el: HTMLElement, binding: any) {
    const handler = (event: MouseEvent) => {
      if (!(el === event.target || el.contains(event.target as Node))) {
        binding.value(event)
      }
    }

    clickOutsideHandlers.set(el, handler)
    document.addEventListener('click', handler)
  },
  unmounted(el: HTMLElement) {
    const handler = clickOutsideHandlers.get(el)
    if (handler) {
      document.removeEventListener('click', handler)
      clickOutsideHandlers.delete(el)
    }
  }
}

// 复制到剪贴板指令
export const vCopy = {
  mounted(el: HTMLElement, binding: any) {
    el.addEventListener('click', () => {
      const text = binding.value || el.textContent

      navigator.clipboard.writeText(text).then(() => {
        // 可以在这里添加成功提示
        console.log('复制成功', text)
      }).catch(err => {
        console.log('复制失败', err)
      })
    })
  }
}

// 安装所有指令
export function setupDirectives(app: App) {
  app.directive('focus', vFocus)
  app.directive('click-outside', vClickOutside)
  app.directive('copy', vCopy)
}
