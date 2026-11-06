import { mergeConfig } from 'vite'
import baseConfig from './vite.config'

const cacheDir = process.env.L1_VITE_CACHE_DIR
if (!cacheDir || !cacheDir.trim()) {
  throw new Error('L1_VITE_CACHE_DIR is required for the L1 Vite config')
}

export default mergeConfig(baseConfig, {
  cacheDir,
  server: {
    host: '127.0.0.1',
    port: 5191,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8031',
        changeOrigin: true,
      },
    },
  },
})
