<template>
  <div class="main-container">
    <!-- 导航栏 -->
    <el-header class="header">
      <div class="header-left">
        <router-link to="/" class="logo">
          <span class="logo-icon">🤖</span>
          <span class="logo-text">{{ appTitle }}</span>
        </router-link>
      </div>

      <div class="header-center">
        <el-menu
          :default-active="activeMenu"
          mode="horizontal"
          router
          class="nav-menu"
        >
          <el-menu-item index="/dashboard">
            <el-icon><House/></el-icon>
            <span>控制面板</span>
          </el-menu-item>
            <el-menu-item index="/characters">
              <el-icon>
                <User/>
              </el-icon>
              <span>AI角色</span>
            </el-menu-item>
          <el-menu-item index="/chat">
            <el-icon><ChatDotRound /></el-icon>
            <span>聊天</span>
          </el-menu-item>
        </el-menu>
      </div>

      <div class="header-right">
        <el-dropdown @command="handleCommand">
          <span class="user-info">
            <el-avatar :size="32" :src="userAvatar">
              {{ userInitials }}
            </el-avatar>
            <span class="username">{{ userInfo?.username }}</span>
            <el-icon><ArrowDown/></el-icon>
          </span>

          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="profile">
                <el-icon>
                  <User/>
                </el-icon>
                个人资料
              </el-dropdown-item>
              <el-dropdown-item divided command="logout">
                <el-icon>
                  <SwitchButton/>
                </el-icon>
                退出登录
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </el-header>

    <!-- 主要内容区域 -->
    <el-main class="main-content">
      <router-view v-slot="{ Component }">
        <component :is="Component"/>
      </router-view>
    </el-main>

    <!-- 页脚 -->
    <el-footer class="footer">
      <div class="footer-content">
        <span> 2024 My AI Assistant. All rights reserved.</span>
        <div class="footer-links">
          <a href="#">关于</a>
          <a href="#">帮助</a>
          <a href="#">隐私政策</a>
        </div>
      </div>
    </el-footer>
  </div>
</template>

<script setup lang="ts">
import {computed} from 'vue'
import {useRoute, useRouter} from "vue-router"
import {ElMessage, ElMessageBox} from "element-plus"
import {House, User, ArrowDown, SwitchButton, ChatDotRound} from "@element-plus/icons-vue"
import {useAuthStore} from "@/stores/auth"

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

const appTitle = import.meta.env.VITE_APP_TITLE || 'My AI Assistant'
const userInfo = computed(() => authStore.userInfo)

// 活动菜单项
const activeMenu = computed(() => route.path)

// 用户头像
const userAvatar = computed(() => '')
const userInitials = computed(() => {
  const name = userInfo.value?.full_name || userInfo.value?.username || ''
  return name.charAt(0).toUpperCase()
})

// 处理下拉菜单命令
const handleCommand = async (command: string) => {
  switch (command) {
    case 'profile':
      // TODO：跳转到个人资料页面
      ElMessage.info('个人资料功能开发中')
      break
    case 'logout':
      await handleLogout()
      break
  }
}

// 处理退出登录
const handleLogout = async () => {
  try {
    await ElMessageBox.confirm('确定要退出吗?', '提示', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning'
    })

    await authStore.logout()
    router.push('/login')
    ElMessage.success('已退出登录')
  } catch (error) {
    // 用户取消
  }
}
</script>

<style scoped lang="scss">
.main-layout {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.1);

  .header-left {
    .logo {
      display: flex;
      align-items: center;
      text-decoration: none;
      color: white;

      .logo-text {
        font-size: 24px;
        margin-right: 8px;
      }

      .logo-text {
        font-size: 20px;
        font-weight: bold;
      }
    }
  }

  .header-center {
    flex: 1;
    display: flex;
    justify-content: center;

    .nav-menu {
      background: transparent;
      border-bottom: none;

      :deep(.el-menu-item) {
        color: rgba(255, 255, 255, 0.8);

        &:hover {
          background: rgba(255, 255, 255, 0.1);
          color: white;
        }

        &.is-active {
          color: white;
          border-bottom-color: white;
        }
      }
    }
  }

  .header-right {
    .user-info {
      display: flex;
      align-items: center;
      cursor: pointer;
      color: white;

      .username {
        margin: 0 8px;
        font-size: 14px;
      }
    }
  }
}

.main-content {
  flex: 1;
  padding: 20px;
  background: #f5f7fa;
}

.footer {
  background: #fff;
  border-top: 1px solid #e4e7ed;
  padding: 16px 20px;

  .footer-content {
    display: flex;
    justify-content: space-between;
    align-items: center;
    color: #909399;
    font-size: 14px;

    .footer-links {
      a {
        color: #909399;
        text-decoration: none;
        margin-left: 16px;

        &:hover {
          color: #409eff;
        }
      }
    }
  }
}

// 过渡动画
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.3s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
