<template>
  <div class="character-detail-container">
    <!-- 页面标题与操作 -->
    <div class="page-header">
      <div class="header-left">
        <el-button text @click="goBack">
          <el-icon><ArrowLeft /></el-icon>
          返回
        </el-button>
        <h1 class="page-title">角色详情</h1>
      </div>
      <div class="header-right">
        <el-button
          :disabled="!character"
          @click="openEdit"
        >
          <el-icon><Edit /></el-icon>
          编辑
        </el-button>
        <el-button
          type="danger"
          plain
          :disabled="!character"
          @click="handleDelete"
        >
          <el-icon><Delete /></el-icon>
          删除
        </el-button>
        <el-button
          type="primary"
          :disabled="!character"
          @click="startConversation"
        >
          <el-icon><ChatDotRound /></el-icon>
          开始对话
        </el-button>
      </div>
    </div>

    <!-- 加载状态 -->
    <div v-if="isLoading" class="loading-state">
      <el-skeleton :rows="6" animated />
    </div>

    <!-- 角色详情 -->
    <el-card v-else-if="character" class="detail-card" shadow="never">
      <div class="detail-header">
        <div class="character-icon"><span class="icon">🤖</span></div>
        <div class="character-meta">
          <h2 class="character-name">{{ character.name }}</h2>
          <el-tag size="small" class="character-model">{{ character.model || '默认模型' }}</el-tag>
        </div>
      </div>

      <el-divider />

      <div class="detail-section">
        <h3 class="section-title">系统提示词</h3>
        <p class="character-prompt">{{ character.system_prompt }}</p>
      </div>

      <div class="detail-stats">
        <div class="stat-item">
          <el-icon><ChatDotRound /></el-icon>
          <span>{{ character.conversation_count }} 对话</span>
        </div>
        <div class="stat-item">
          <el-icon><Clock /></el-icon>
          <span>创建于 {{ formatDate(character.created_at) }}</span>
        </div>
      </div>
    </el-card>

    <!-- 不存在 -->
    <el-empty v-else description="角色不存在或已被删除" />

    <!-- 编辑角色对话框 -->
    <el-dialog
      v-model="editDialogVisible"
      title="编辑AI角色"
      width="600px"
    >
      <el-form ref="editFormRef" :model="editForm" :rules="editRules" label-width="100px">
        <el-form-item label="角色名称" prop="name">
          <el-input v-model="editForm.name" placeholder="例如：python导师、代码助手" />
        </el-form-item>
        <el-form-item label="系统提示" prop="system_prompt">
          <el-input v-model="editForm.system_prompt" :rows="4" placeholder="定义角色的行为和个性..." maxlength="1000" show-word-limit />
        </el-form-item>
        <el-form-item label="AI模型" prop="model">
          <el-input v-model="editForm.model" placeholder="可选，留空则用系统默认模型；可填写真实模型名，如 deepseek-v3、qwen-plus" clearable maxlength="100" />
          <div class="form-tip">
            <el-icon><InfoFilled /></el-icon>
            留空将使用系统默认模型；若填写模型，必须同时填写下方 API Key
          </div>
        </el-form-item>
        <el-form-item label="API密钥" prop="api_key">
          <el-input v-model="editForm.api_key" type="password" placeholder="留空将清除原密钥；填写则替换（须与模型同填）" show-password />
          <div class="form-tip">
            <el-icon><InfoFilled /></el-icon>
            留空 = 清除原 API Key 并改用默认配置；若上方已填模型则必须同时填写
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="editing" @click="handleEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { ArrowLeft, ChatDotRound, Clock, Edit, Delete, InfoFilled } from '@element-plus/icons-vue'
import { useCharacterStore } from '@/stores/character'
import type { Character, CharacterUpdate } from '@/types/character'

const route = useRoute()
const router = useRouter()
const characterStore = useCharacterStore()

const character = ref<Character | null>(null)
const isLoading = ref(false)

const characterId = computed(() => route.params.id as string)

onMounted(async () => {
  await loadCharacter()
})

const loadCharacter = async () => {
  isLoading.value = true
  try {
    await characterStore.fetchCharacter(characterId.value)
    const c = characterStore.currentCharacter as any
    // 校验返回的确实是有效角色对象（拦截器异常时可能为 error 对象）
    character.value = (c && c.id && c.name) ? (c as Character) : null
  } catch {
    character.value = null
  } finally {
    isLoading.value = false
  }
}

const goBack = () => router.push('/characters')
const startConversation = () => router.push(`/chat/${characterId.value}`)

// 编辑
const editDialogVisible = ref(false)
const editing = ref(false)
const editFormRef = ref<FormInstance>()
const editForm = ref<CharacterUpdate>({
  name: '',
  system_prompt: '',
  model: '',
  api_key: ''
})

const editRules: FormRules = {
  name: [
    { required: true, message: '请输入角色名称', trigger: 'blur' },
    { min: 2, max: 50, message: '长度在 2 到 50 个字符', trigger: 'blur' }
  ],
  system_prompt: [
    { required: true, message: '请输入系统提示', trigger: 'blur' },
    { min: 10, max: 1000, message: '长度在 10 到 1000 个字符', trigger: 'blur' }
  ]
}

const openEdit = () => {
  if (!character.value) return
  editForm.value = {
    name: character.value.name,
    system_prompt: character.value.system_prompt,
    model: character.value.model || '',
    api_key: ''
  }
  editDialogVisible.value = true
}

const handleEdit = async () => {
  if (!editFormRef.value || !character.value) return
  const isValid = await editFormRef.value.validate()
  if (!isValid) return
  editing.value = true
  try {
    await characterStore.updateCharacter(character.value.id, editForm.value)
    ElMessage.success('角色更新成功')
    editDialogVisible.value = false
    await loadCharacter()
  } catch (error: any) {
    ElMessage.error(error.message || '更新失败')
  } finally {
    editing.value = false
  }
}

const handleDelete = async () => {
  if (!character.value) return
  try {
    await ElMessageBox.confirm(
      `确定要删除角色 "${character.value.name}" 吗？该角色的全部对话历史也将被删除，且不可恢复。`,
      '确认删除',
      { confirmButtonText: '确定删除', cancelButtonText: '取消', type: 'warning' }
    )
    await characterStore.deleteCharacter(character.value.id)
    ElMessage.success('删除成功')
    router.push('/characters')
  } catch {
    // 用户取消
  }
}

const formatDate = (dateString?: string) => {
  if (!dateString) return '未知'
  return new Date(dateString).toLocaleDateString('zh-CN')
}
</script>

<style scoped lang="scss">
.character-detail-container {
  padding: 20px;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 24px;

  .header-left {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .page-title {
    font-size: 22px;
    font-weight: 600;
    color: #303133;
    margin: 0;
  }
}

.detail-card {
  max-width: 720px;
  border-radius: 12px;

  .detail-header {
    display: flex;
    align-items: center;
    gap: 16px;

    .character-icon {
      width: 56px;
      height: 56px;
      border-radius: 50%;
      background: #f5f7fa;
      display: flex;
      align-items: center;
      justify-content: center;

      .icon {
        font-size: 32px;
      }
    }

    .character-meta {
      .character-name {
        margin: 0 0 8px;
        font-size: 20px;
        font-weight: 600;
        color: #303133;
      }
    }
  }

  .detail-section {
    .section-title {
      font-size: 15px;
      color: #606266;
      margin: 0 0 10px;
    }

    .character-prompt {
      color: #303133;
      font-size: 14px;
      line-height: 1.7;
      white-space: pre-wrap;
      background: #f5f7fa;
      padding: 14px 16px;
      border-radius: 8px;
      margin: 0;
    }
  }

  .detail-stats {
    display: flex;
    gap: 24px;
    margin-top: 20px;

    .stat-item {
      display: flex;
      align-items: center;
      gap: 6px;
      color: #909399;
      font-size: 13px;
    }
  }
}

.loading-state {
  max-width: 720px;
}

// 对话框表单提示
.form-tip {
  margin-top: 8px;
  font-size: 12px;
  color: #909399;
  display: flex;
  align-items: center;

  .el-icon {
    margin-right: 4px;
  }
}
</style>
