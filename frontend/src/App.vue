<!--<template>-->
<!--  <MainLayout v-if="showLayout">-->
<!--    <router-view />-->
<!--  </MainLayout>-->
<!--  <router-view v-else />-->
<!--</template>-->

<!--<script setup lang="ts">-->
<!--import { computed } from 'vue'-->
<!--import { useRoute } from "vue-router";-->
<!--import { useAuthStore } from "@/stores/auth";-->
<!--import MainLayout from "@/layouts/MainLayout.vue";-->

<!--const route = useRoute()-->
<!--const authStore = useAuthStore()-->
<!--const isAuthenticated = computed(() => {-->
<!--  // 确保store已初始化-->
<!--  return !!authStore.token || !!localStorage.getItem("access_token")-->
<!--})-->

<!--// 根据路由元信息判断是否显示布局-->
<!--const showLayout = computed(() => {-->
<!--  // 只有当用户已认证且路由需要布局时猜显示MainLayout-->
<!--  // 这里假设所有认证的路由都需要布局（除了登录/注册）-->
<!--  return route.meta.showLayout !== false-->
<!--})-->
<!--</script>-->

<!--<style>-->
<!--/* 全局样式 */-->
<!--* {-->
<!--  margin: 0;-->
<!--  padding: 0;-->
<!--  box-sizing: border-box;-->
<!--}-->

<!--body {-->
<!--  font-family: 'Helvetica Neue', Helvetica, 'PingFang SC',-->
<!--  'Hiragino Sans GB','Microsoft YaHei','微软雅黑', Arial, sans-serif;-->
<!--  -webkit-font-smoothing: antialiased;-->
<!--  -moz-osx-font-smoothing: grayscale;-->
<!--}-->

<!--#app {-->
<!--  min-height: 100vh;-->
<!--}-->

<!--/* 自定义滚动条 */-->
<!--::-webkit-scrollbar {-->
<!--  width: 8px;-->
<!--  height: 8px;-->
<!--}-->

<!--::-webkit-scrollbar-track {-->
<!--  background: #f1f1f1;-->
<!--  border-radius: 4px;-->
<!--}-->

<!--::-webkit-scrollbar-thumb {-->
<!--  background: #c1c1c1;-->
<!--  border-radius: 4px;-->
<!--}-->

<!--::-webkit-scrollbar-thumb:hover {-->
<!--  background: #a8a8a8;-->
<!--}-->
<!--</style>-->

<template>
  <router-view />
</template>

<script setup lang="ts">
// 不需要任何逻辑，路由会自动根据 meta.showLayout 决定显示哪个布局
</script>

<style>
/* 全局样式保持不变 */
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

/* 自定义滚动条 */
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
