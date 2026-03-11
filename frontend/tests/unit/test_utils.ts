import { describe, it, expect } from 'vitest'
import { formatFileSize, truncateText, debounce, throttle } from 'frontend/src/utils'

describe('Utils', () => {
  describe('formatFileSize', () => {
    it('should format bytes correctly', () => {
      expect(formatFileSize(0)).toBe('0 B')
      expect(formatFileSize(1024)).toBe('1.00 KB')
      expect(formatFileSize(1048576)).toBe('1.00 MB')
      expect(formatFileSize(1073741824)).toBe('1.00 GB')
    })
  })

  describe('truncateText', () => {
    it('should truncate text correctly', () => {
      const text = 'This is a long text'
      expect(truncateText(text, 10)).toBe('This is a...')
      expect(truncateText(text, 20)).toBe('This is a long text')
      expect(truncateText('short', 10)).toBe('short')
    })
  })

  describe('debounce', () => {
    it('should debounce function calls', async () => {
      let count = 0
      const fn = () => { count++ }
      const debounced = debounce(fn, 100)
      debounced()
      debounced()
      debounced()

      expect(count).toBe(0)

      await new Promise(resolve => setTimeout(resolve, 150))
      expect(count).toBe(1)
    })
  })

  describe('throttle', () => {
    it('should throttle function calls', async () => {
      let count = 0
      const fn = () => { count++ }
      const throttled = throttle(fn, 100)
      throttled()
      throttled()
      throttled()

      expect(count).toBe(1)

      await new Promise(resolve => setTimeout(resolve, 150))
      throttled()
      expect(count).toBe(2)
    })
  })
})