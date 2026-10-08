import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueJsx from '@vitejs/plugin-vue-jsx'

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
    port: 5173,
    proxy: {
      '/api': {
        // 本副本 Windows 开发后端为 8021；Docker 容器内部仍是 8000（勿同步修改）。
        target: 'http://127.0.0.1:8021',
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
