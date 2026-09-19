<template>
  <div class="characters-container">
    <!-- 页面标题和操作按钮 -->
    <div class="page-header">
      <div class="header-left">
        <h1 class="page-title">AI角色管理</h1>
        <p class="page-subtitle">创建和管理您的AI角色助手</p>
      </div>
      <div class="header-right">
        <el-button
          type="primary"
          size="large"
          @click="showCreateDialog = true"
        >
          <el-icon><Plus /></el-icon>
          创建新角色
        </el-button>
      </div>
    </div>

    <!-- 角色列表 -->
    <div class="characters-grid">
      <el-card
        v-for="character in characters"
        :key="character.id"
        class="character-card"
        shadow="hover"

        @click="viewCharacter(character.id)"
      >
        <template #header>
          <div class="card-header">
            <div class="character-icon">
              <span class="icon"></span>
            </div>
            <div class="character-info">
              <h3 class="character-name">{{ character.name }}</h3>
              <p class="character-model">{{ character.model || '默认模型' }}</p>
            </div>
            <div class="card-actions">
              <el-dropdown
                @command="handleCommand($event, character)">
                <el-icon><More /></el-icon>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="edit">编辑</el-dropdown-item>
                    <el-dropdown-item command="delete">删除</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </div>
        </template>

        <div class="card-content">
          <p class="character-prompt">{{ truncatePrompt(character.system_prompt) }}</p>

          <div class="character-stats">
            <div class="stat-item">
              <el-icon><ChatDotRound /></el-icon>
              <span>{{ character.conversation_count }} 对话</span>
            </div>
            <div class="stat-item">
              <el-icon><Clock /></el-icon>

              <span>{{ formatDate(character.created_at) }}</span>
            </div>
          </div>
        </div>

        <template #footer>
          <el-button
            type="primary"
            plain
            size="small"

            @click.stop="startConversation(character.id)"
          >
            开始对话
          </el-button>
          <el-button
            type="info"
            plain
            size="small"

            @click.stop="viewCharacter(character.id)"
          >
            查看详情
          </el-button>
        </template>
      </el-card>

      <!-- 空状态 -->
      <div v-if="characters.length === 0 && !characterStore.isLoading" class="empty-state">
        <div class="empty-icon"></div>
        <h3>还没有AI角色</h3>
        <p>创建您的第一个AI助手角色，开始只能对话</p>
        <el-button type="primary" @click="showCreateDialog = true">
          创建第一个角色
        </el-button>
      </div>
    </div>

    <!-- 加载状态 -->
    <div v-if="characterStore.isLoading" class="loading-state">
      <el-skeleton :rows="3" animated />
    </div>

    <!-- 创建角色对话框 -->
    <el-dialog
      v-model="showCreateDialog"
      title="创建AI角色"
      width="600px"
      @close="resetForm"
    >
      <el-form
        ref="createFormRef"
        :model="createForm"
        :rules="createRules"
        label-width="100px"
      >
        <el-form-item label="角色名称" prop="name">
          <el-input
            v-model="createForm.name"
            placeholder="例如：python导师、代码助手"
          />
        </el-form-item>

        <el-form-item label="系统提示" prop="system_prompt">
          <el-input
            v-model="createForm.system_prompt"
            :rows="4"
            placeholder="定义角色的行为和个性..."
            maxlength="1000"
            show-word-limit
          />
        </el-form-item>

        <el-form-item label="AI模型" prop="model">
          <el-input
            v-model="createForm.model"
            placeholder="可选，留空则用系统默认模型；可填写真实模型名，如 deepseek-v3、qwen-plus"
            clearable
            maxlength="100"
          />
          <div class="form-tip">
            <el-icon><InfoFilled /></el-icon>
            留空将使用系统默认模型；若填写模型，必须同时填写下方 API Key
          </div>
        </el-form-item>

        <el-form-item
          label="API密钥"
          prop="api_key">
          <el-input
            v-model="createForm.api_key"
            type="password"
            placeholder="可选，如果不填将使用默认密钥"
            show-password
          />
          <div class="form-tip">
            <el-icon><InfoFilled /></el-icon>
            如果不填写，将使用系统默认的API密钥
          </div>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button
          @click="showCreateDialog = false">取消</el-button>
        <el-button
          type="primary"
          :loading="creating"
          @click="handleCreate">
          创建
        </el-button>
      </template>
    </el-dialog>

    <!-- 编辑角色对话框 -->
    <el-dialog
      v-model="editDialogVisible"
      title="编辑AI角色"
      width="600px"
      @close="resetEditForm"
    >
      <el-form
        ref="editFormRef"
        :model="editForm"
        :rules="editRules"
        label-width="100px"
      >
        <el-form-item label="角色名称" prop="name">
          <el-input v-model="editForm.name" placeholder="例如：python导师、代码助手" />
        </el-form-item>

        <el-form-item label="系统提示" prop="system_prompt">
          <el-input
            v-model="editForm.system_prompt"
            :rows="4"
            placeholder="定义角色的行为和个性..."
            maxlength="1000"
            show-word-limit
          />
        </el-form-item>

        <el-form-item label="AI模型" prop="model">
          <el-input
            v-model="editForm.model"
            placeholder="可选，留空则用系统默认模型；可填写真实模型名，如 deepseek-v3、qwen-plus"
            clearable
            maxlength="100"
          />
          <div class="form-tip">
            <el-icon><InfoFilled /></el-icon>
            留空将使用系统默认模型；若填写模型，必须同时填写下方 API Key
          </div>
        </el-form-item>

        <el-form-item label="API密钥" prop="api_key">
          <el-input
            v-model="editForm.api_key"
            type="password"
            placeholder="留空将清除原密钥；填写则替换（须与模型同填）"
            show-password
          />
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
import { ref, onMounted, watch } from "vue"
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from "element-plus"
import {
  Plus,
  More,
  ChatDotRound,
  Clock,
  InfoFilled
} from "@element-plus/icons-vue"
import { useCharacterStore } from "@/stores/character"
import type { Character, CharacterCreate, CharacterUpdate } from '@/types/character'
import { storeToRefs } from 'pinia'

const router = useRouter()
const characterStore = useCharacterStore()
const { characters } = storeToRefs(characterStore)

// 对话框状态
const showCreateDialog = ref(false)
const creating = ref(false)

// 编辑对话框状态
const editDialogVisible = ref(false)
const editing = ref(false)
const editingCharacterId = ref<string | null>(null)
const editFormRef = ref<FormInstance>()
const editForm = ref<CharacterUpdate>({
  name: '',
  system_prompt: '',
  model: '',
  api_key: ''
})

// 创建表单
const createFormRef = ref<FormInstance>()
const createForm = ref<CharacterCreate>({
  name: '',
  system_prompt: '',
  model: '',
  api_key: ''
})

// 表单验证规则
// 模型 / API Key 必须同时填写或同时留空（方案 A，创建与编辑共用）
const makePairValidator = (form: { value: { model?: string; api_key?: string } }) => (
  _rule: any,
  _value: any,
  callback: any
) => {
  const modelFilled = (form.value.model || '').trim().length > 0
  const keyFilled = (form.value.api_key || '').trim().length > 0
  if (modelFilled !== keyFilled) {
    callback(new Error('模型和 API Key 必须同时填写或同时留空'))
    return
  }
  callback()
}

const createRules: FormRules = {
  name: [
    { required: true, message: '请输入角色名称', trigger: 'blur' },
    { min: 2, max: 50, message: '长度在 2 到 50 个字符', trigger: 'blur' }
  ],
  system_prompt: [
    { required: true, message: '请输入系统提示', trigger: 'blur' },
    { min: 10, max: 1000, message: '长度在 10 到 1000 个字符', trigger: 'blur' }
  ],
  model: [
    { validator: makePairValidator(createForm), trigger: ['blur', 'change'] }
  ],
  api_key: [
    { validator: makePairValidator(createForm), trigger: ['blur', 'change'] }
  ]
}

const editRules: FormRules = {
  name: [
    { required: true, message: '请输入角色名称', trigger: 'blur' },
    { min: 2, max: 50, message: '长度在 2 到 50 个字符', trigger: 'blur' }
  ],
  system_prompt: [
    { required: true, message: '请输入系统提示', trigger: 'blur' },
    { min: 10, max: 1000, message: '长度在 10 到 1000 个字符', trigger: 'blur' }
  ],
  model: [
    { validator: makePairValidator(editForm), trigger: ['blur', 'change'] }
  ],
  api_key: [
    { validator: makePairValidator(editForm), trigger: ['blur', 'change'] }
  ]
}

// 模型 / API Key 联动校验：任一侧变动时同步校验另一侧
watch(() => createForm.value.model, () => {
  createFormRef.value?.validateField('api_key')
})
watch(() => createForm.value.api_key, () => {
  createFormRef.value?.validateField('model')
})
watch(() => editForm.value.model, () => {
  editFormRef.value?.validateField('api_key')
})
watch(() => editForm.value.api_key, () => {
  editFormRef.value?.validateField('model')
})

// 组件挂载时加载数据
onMounted(() => { loadCharacters() })

// 加载角色列表
const loadCharacters = async () => {
  try {
    await characterStore.fetchCharacters()
  } catch (error) {
    ElMessage.error('加载角色列表失败')
  }
}

// 查看角色详情
const viewCharacter = (id: string) => {
  router.push(`/characters/${id}`)
}

// 开始对话
const startConversation = (id: string) => {
  // TODO：跳转到对话页面
  router.push(`/chat/${id}`)
}

// 处理下拉菜单命令
const handleCommand = async (command: string, character: Character) => {
  switch (command) {
    case 'edit':
      openEdit(character)
      break
    case 'delete':
      await handleDelete(character)
      break
  }
}

// 删除角色
const handleDelete = async (character: Character) => {
  try {
    await ElMessageBox.confirm(
      '确定要删除角色 "${character.name}" 吗?',
      '确认删除',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )

    await characterStore.deleteCharacter(character.id)
    ElMessage.success('删除成功')
  } catch (error) {
    // 用户取消
  }
}

// 创建角色
const handleCreate = async () => {
  if (!createFormRef.value) return

  // 验证表单
  const isValid = await createFormRef.value.validate()
  if (!isValid) return

  creating.value = true

  try {
    await characterStore.createCharacter(createForm.value)
    ElMessage.success('角色创建成功')
    showCreateDialog.value = false
    resetForm()
  } catch (error: any) {
    ElMessage.error(error.message || '创建失败')
  } finally {
    creating.value = false
  }
}

// 重置表单
const resetForm = () => {
  if (createFormRef.value) {
    createFormRef.value.resetFields()
  }
}

// 打开编辑对话框（预填当前角色；API Key 不回显，留空按方案 B 清空原 Key）
const openEdit = (character: Character) => {
  editingCharacterId.value = character.id
  editForm.value = {
    name: character.name,
    system_prompt: character.system_prompt,
    model: character.model || '',
    api_key: ''
  }
  editDialogVisible.value = true
}

// 提交编辑
const handleEdit = async () => {
  if (!editFormRef.value || !editingCharacterId.value) return
  const isValid = await editFormRef.value.validate()
  if (!isValid) return
  editing.value = true
  try {
    await characterStore.updateCharacter(editingCharacterId.value, editForm.value)
    ElMessage.success('角色更新成功')
    editDialogVisible.value = false
    await loadCharacters()
  } catch (error: any) {
    ElMessage.error(error.message || '更新失败')
  } finally {
    editing.value = false
  }
}

// 编辑弹窗关闭时重置
const resetEditForm = () => {
  if (editFormRef.value) {
    editFormRef.value.resetFields()
  }
}

// 工具函数
const truncatePrompt = (prompt: string, length: number = 100) => {
  if (prompt.length <= length)
return prompt
  return prompt.substring(0, length) + '...'
}

const formatDate = (dateString?: string) => {
  if (!dateString) return '未知时间'
  const date = new Date(dateString)
  return date.toLocaleDateString('zh-CN')
}
</script>

<style scoped lang="scss">
.characters-container {
  padding: 20px;
}

.page-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 30px;

  .header-left {
    .page-title {
      font-size: 24px;
      font-weight: 600;
      color: #303133;
      margin-bottom: 8px;
    }

    .page-subtitle {
      color: #909399;
      font-size: 14px;
    }
  }
}

.characters-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 20px;

  .character-card {
    cursor: pointer;
    transition: transform 0.3s ease;

    &:hover {
      transform: translateY(-4px);
    }

    :deep(.el-card__header) {
      padding: 20px 20px 10px;
      border-bottom: none;
    }

    .card-header {
      display: flex;
      align-items: center;

      .character-icon {
        margin-right: 12px;

        .icon {
          font-size: 32px;
        }
      }

      .character-info {
        flex: 1;

        .character-name {
          font-size: 16px;
          font-weight: 600;
          color: #303133;
          margin-bottom: 4px;
        }

        .character-model {
          font-size: 12px;
          color: #909399;
          background: #f5f7fa;
          padding: 2px 8px;
          border-radius: 10px;
          display: inline-block;
        }
      }

      .card-actions {
        :deep(.el-dropdown) {
          cursor: pointer;
          padding: 4px;

          &:hover {
            background: #f5f7fa;
            border-radius: 4px;
          }
        }
      }
    }

    .card-content {
      .character-prompt {
        color: #606266;
        font-size: 14px;
        line-height: 1.5;
        margin-bottom: 16px;
      }

      .character-stats {
        display: flex;
        gap: 16px;

        .stat-item {
          display: flex;
          align-items: center;
          color: #909399;
          font-size: 12px;

          .el-icon {
            margin-right: 4px;
            font-size: 14px;
          }
        }
      }
    }

    :deep(.el-card__footer) {
      display: flex;
      gap: 8px;
      padding: 16px 20px;
    }
  }
}

.empty-state {
  grid-column: 1 / -1;
  text-align: center;
  padding: 60px 20px;

  .empty-icon {
    font-size: 64px;
    margin-bottom: 20px;
  }

  h3 {
    font-size: 18px;
    color: #303133;
    margin-bottom: 8px;
  }

  p {
    color: #909399;
    margin-bottom: 20px;
  }
}

.loading-state {
  grid-column: 1 / -1;
  padding: 40px 20px;
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

// 响应式设计
@media (max-width: 768px) {
  .page-header {
    flex-direction: column;
    gap: 16px;
  }

  .characters-grid {
    grid-template-columns: 1fr;
  }
}
</style>
