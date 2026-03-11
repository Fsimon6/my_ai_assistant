<template>
  <div class="login-container">
    <div class="login-box">
      <!-- 左侧装饰 -->
      <div class="login-left">
        <div class="welcome-text">
          <h1>欢迎回来</h1>
          <p>登录您的AI助手账户，开始智能对话之旅</p >
        </div>
        <div class="illustration">
          <div class="robot-icon">🤖</div>
          <p class="illustration-text">智能对话，无限可能</p >
        </div>
      </div>

      <!-- 右侧表单 -->
      <div class="login-right">
        <div class="form-header">
          <h2>登录账户</h2>
          <p>输入您的凭据以继续</p >
        </div>

        <el-form
          ref="loginFormRef"
          :model="loginForm"
          :rules="loginRules"
          class="login-form"
          @submit.prevent="handleLogin"
        >
          <el-form-item prop="username">
            <el-input
              v-model="loginForm.username"
              placeholder="用户名或邮箱"
              size="large"
              :prefix-icon="User"
            />
          </el-form-item>

          <el-form-item prop="password">
            <el-input
              v-model="loginForm.password"
              type="password"
              placeholder="密码"
              size="large"
              :prefix-icon="Lock"
              show-password
            />
          </el-form-item>

          <div class="form-options">
            <el-checkbox v-model="rememberMe">记住我</el-checkbox>
            <router-link to="/forgot-password" class="forgot-password">
              忘记密码？
            </router-link>
          </div>

          <el-form-item>
            <el-button
              type="primary"
              size="large"
              :loading="loading"
              @click="handleLogin"
              class="login-button"
            >
              {{ loading ? '登录中...' : '登录' }}
            </el-button>
          </el-form-item>

          <div class="divider">
            <span>或者</span>
          </div>

          <div class="register-link">
            还没有账户？
            <router-link to="/register">立即注册</router-link>
          </div>
        </el-form>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive }from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from "element-plus"
import { User, Lock } from '@element-plus/icons-vue'
import { useAuthStore} from '@/stores/auth'

const router = useRouter()
const authStore = useAuthStore()

// 表单引用
const loginFormRef = ref<FormInstance>()

// 表单数据
const loginForm = reactive({
  username: '',
  password: ''
})

const rememberMe = ref(false)
const loading = ref(false)

// 表单验证规则
const loginRules: FormRules = {
  username: [
    { required: true, message: '请输入用户名或邮箱', trigger: 'blur' },
    { min: 3, message: '用户名至少3个字符', trigger: 'blur'}
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur'},
    { min: 6, message: '密码至少6个字符', trigger: 'blur' }
  ]
}

// 处理登录 - 暂时模拟
const handleLogin = async () => {
  if (!loginFormRef.value) return

  // 验证表单
  const isvalid = await loginFormRef.value.validate()
  if (!isvalid) return

  loading.value = true

  try {
    // TODO:真实API调用
    // const result = await authStore.login(loginForm.username, loginForm.password)

    // 模拟成功登录
    const mockToken = 'mock-jwt-token-for-texting'
    const mockUser = {
      id: 1,
      username: loginForm.username,
      email: '${loginForm.username} @text.com',
      is_active: true,
      is_superuser: false,
      created_at: new Date().toISOString()
    }

    // 保存到localStorage
    localStorage.setItem('access_token', mockToken)
    localStorage.setItem('user_info', JSON.stringify(mockUser))

    // 更新store状态
    authStore.token = mockToken
    authStore.userInfo = mockUser

    ElMessage.success('登录成功（模拟）')
    router.push('/dashboard')
  } catch (error: any) {
    ElMessage.error(error.message || '登录失败，请重试')
  } finally {
    loading.value = false
  }
}

// 记住我功能
if (rememberMe.value) {
  const savedUsername = localStorage.getItem('remember_username')
  if (savedUsername) {
    loginForm.username = savedUsername
  }
}
</script>

<style scoped lang="scss">
.login-container {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
  padding: 20px;
}

.login-box {
  display: flex;
  width: 900px;
  background: white;
  border-radius: 16px;
  overflow: hidden;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.1);
}

.login-left {
  flex: 1;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  padding: 60px 40px;
  display: flex;
  flex-direction: column;
  justify-content: center;

  .welcome-text {
    margin-bottom: 60px;

    h1 {
      font-size: 32px;
      margin-bottom: 16px;
      font-weight: 600;
    }

    p {
      font-size: 16px;
      opacity: 0.9;
      line-height: 1.6;
    }
  }

  .illustration {
    text-align: center;


    .robot-icon {
      font-size: 80px;
      margin-bottom: 20px;
    }

    .illustration-text {
      font-size: 18px;
      font-weight: 500;
    }
  }
}

.login-right {
  flex: 1;
  padding: 60px 40px;

  .form-header {
    text-align: center;
    margin-bottom: 40px;

    h2 {
      font-size: 28px;
      color: #303133;
      margin-bottom: 8px;
      font-weight: 600;
    }

    p {
      color: #909399;
      font-size: 14px;
    }
  }

  .login-form {
    .form-options {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 24px;

      .forgot-password {
        color: #409eff;
        text-decoration: none;
        font-size: 14px;

        &:hover {
          text-decoration: underline;
        }
      }
    }

    .login-button {
      width: 100%;
      height: 48px;
      font-size: 16px;
      border-radius: 8px;
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      border: none;

      &:hover {
        opacity: 0.9;
      }
    }
  }

  .divider {
    display: flex;
    align-items: center;
    margin: 24px 0;
    color: #c0c4cc;

    &::before
    &::after {
      content: '';
      flex: 1;
      height: 1px;
      background: #dcdfe6;
    }

    span {
      padding: 0 16px;
      font-size: 14px;
    }
  }

  .register-link {
    text-align: center;
    color: #606266;
    font-size: 14px;

    a {
      color: #409eff;
      text-decoration: none;
      margin-left: 4px;

      &:hover {
        text-decoration: underline;
      }
    }
  }
}

// 响应式设计
@media (max-width: 768px) {
  .login-box {
    flex-direction: column;
    width: 100%;
    max-width: 400px;
  }

  .login-left {
    padding: 40px 20px;

    .welcome-text {
      margin-bottom: 40px;

      h1 {
        font-size: 24px;
      }
    }

    .illustration .robot-icon {
      font-size: 60px;
    }
  }

  .login-right {
    padding: 40px 20px;
  }
}
</style>
