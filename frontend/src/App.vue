<template>
  <div v-if="isL1Testing" class="l1-testing-banner" role="status">{{ l1BannerText }}</div>
  <MainLayout v-if="showLayout">
    <router-view />
  </MainLayout>
  <router-view v-else />
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import MainLayout from '@/layouts/MainLayout.vue'
import { isL1Testing, l1BannerText } from '@/utils/environment'

const route = useRoute()
// 仅当路由 meta.showLayout === true 时渲染主布局（含全局导航栏）
const showLayout = computed(() => route.meta.showLayout === true)
</script>

<style>
.l1-testing-banner {
  position: sticky;
  top: 0;
  z-index: 10000;
  padding: 8px 14px;
  background: #b00020;
  color: #fff;
  font-weight: 700;
  text-align: center;
  letter-spacing: 0.4px;
}

* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

body {
  font-family: 'Helvetica Neue', Helvetica, 'PingFang SC',
  'Hiragino Sans GB','Microsoft YaHei','微软雅黑', Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

#app {
  min-height: 100vh;
}

::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}

::-webkit-scrollbar-track {
  background: #f1f1f1;
  border-radius: 4px;
}

::-webkit-scrollbar-thumb {
  background: #c1c1c1;
  border-radius: 4px;
}

::-webkit-scrollbar-thumb:hover {
  background: #a8a8a8;
}
</style>
