import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueJsx from '@vitejs/plugin-vue-jsx'

// 本地开发默认仍指向 127.0.0.1:8021；Docker 开发由 Compose 注入
// backend-dev:8000，避免容器内 127.0.0.1 指向前端容器自身。
const devProxyTarget = process.env.DEV_PROXY_TARGET || 'http://127.0.0.1:8021'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [
    vue() as any,
    vueJsx() as any,
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    }
  },
  server: {
    // 开发端口统一为 5181（与 README / start*.ps1 / .env.development 一致）
    port: 5181,
    proxy: {
      '/api': {
        // 本地开发默认 http://127.0.0.1:8021；Docker 开发使用
        // DEV_PROXY_TARGET=http://backend-dev:8000。
        target: devProxyTarget,
        changeOrigin: true
        // 后端路由前缀本身就是 /api/v1，无需 rewrite；
        // 原 rewrite 会把 /api/v1 剥成 /v1 导致 404，故删除。
      }
    }
  },
  // css: {
  //   preprocessorOptions: {
  //     scss: {
  //       additionalData: `@use "@/styles/element/index.scss" as *;`
  //     }
  //   }
  // }
})
