<template>
  <div class="register-container">
    <div class="register-box">
      <div class="register-left">
        <div class="welcome-text">
          <h1>创建账户</h1>
          <p>注册您的AI助手账户，开启智能对话之旅</p>
        </div>
        <div class="illustration">
          <div class="robot-icon">🤖</div>
          <p class="illustration-text">智能对话，无限可能</p>
        </div>
      </div>

      <div class="register-right">
        <div class="form-header">
          <h2>注册账户</h2>
          <p>填写以下信息以创建账户</p>
        </div>

        <el-form
          ref="registerFormRef"
          :model="registerForm"
          :rules="registerRules"
          class="register-form"
          @submit.prevent="handleRegister"
        >
          <el-form-item prop="username">
            <el-input
              v-model="registerForm.username"
              placeholder="用户名（至少3个字符）"
              size="large"
              :prefix-icon="User"
            />
          </el-form-item>

          <el-form-item prop="email">
            <el-input
              v-model="registerForm.email"
              placeholder="邮箱"
              size="large"
              :prefix-icon="Message"
            />
          </el-form-item>

          <el-form-item prop="full_name">
            <el-input
              v-model="registerForm.full_name"
              placeholder="昵称（可选）"
              size="large"
              :prefix-icon="UserFilled"
            />
          </el-form-item>

          <el-form-item prop="password">
            <el-input
              v-model="registerForm.password"
              type="password"
              placeholder="密码（至少6个字符）"
              size="large"
              :prefix-icon="Lock"
              show-password
            />
          </el-form-item>

          <el-form-item prop="confirmPassword">
            <el-input
              v-model="registerForm.confirmPassword"
              type="password"
              placeholder="确认密码"
              size="large"
              :prefix-icon="Lock"
              show-password
            />
          </el-form-item>

          <el-form-item>
            <el-button
              type="primary"
              size="large"
              :loading="loading"
              @click="handleRegister"
              class="register-button"
            >
              {{ loading ? '注册中...' : '注册' }}
            </el-button>
          </el-form-item>

          <div class="login-link">
            已有账户？<router-link to="/login">立即登录</router-link>
          </div>
        </el-form>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { User, Lock, Message, UserFilled } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const authStore = useAuthStore()

const registerFormRef = ref<FormInstance>()

const registerForm = reactive({
  username: '',
  email: '',
  full_name: '',
  password: '',
  confirmPassword: ''
})

const registerRules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, message: '用户名至少3个字符', trigger: 'blur' }
  ],
  email: [
    { required: true, message: '请输入邮箱', trigger: 'blur' },
    { type: 'email', message: '邮箱格式不正确', trigger: 'blur' }
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码至少6个字符', trigger: 'blur' }
  ],
  confirmPassword: [
    { required: true, message: '请确认密码', trigger: 'blur' },
    {
      validator: (rule, value, callback) => {
        if (value !== registerForm.password) {
          callback(new Error('两次输入的密码不一致'))
        } else {
          callback()
        }
      },
      trigger: 'blur'
    }
  ]
}

const loading = ref(false)

const handleRegister = async () => {
  if (!registerFormRef.value) return
  const isvalid = await registerFormRef.value.validate()
  if (!isvalid) return

  loading.value = true
  try {
    const result = await authStore.register({
      username: registerForm.username,
      email: registerForm.email,
      password: registerForm.password,
      full_name: registerForm.full_name || undefined
    })
    if (result.success) {
      ElMessage.success('注册成功，请登录')
      router.push('/login')
    } else {
      ElMessage.error(result.text || '注册失败，请重试')
    }
  } catch (error: any) {
    ElMessage.error(error?.message || '注册失败，请重试')
  } finally {
    loading.value = false
  }
}
</script>

<style scoped lang="scss">
.register-container {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
  padding: 20px;
}

.register-box {
  display: flex;
  width: 900px;
  background: white;
  border-radius: 16px;
  overflow: hidden;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.1);
}

.register-left {
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

.register-right {
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

  .register-form {
    .register-button {
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

  .login-link {
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

@media (max-width: 768px) {
  .register-box {
    flex-direction: column;
    width: 100%;
    max-width: 400px;
  }

  .register-left {
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

  .register-right {
    padding: 40px 20px;
  }
}
</style>
