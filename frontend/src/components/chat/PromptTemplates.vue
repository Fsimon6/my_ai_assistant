
---

### 2. **frontend/src/components/chat/ 补充**

#### **frontend/src/components/chat/PromptTemplates.vue**
```vue
<template>
  <div class="prompt-templates">
    <el-card class="templates-card">
      <template #header>
        <div class="card-header">
          <span>提示词模板</span>
          <el-button type="primary" size="small" @click="showNewTemplateDialog">
            新建模板
          </el-button>
        </div>
      </template>

      <!-- 模板列表 -->
      <el-tabs v-model="activeTab" @tab-click="handleTabClick">
        <el-tab-pane label="系统模板" name="system">
          <el-table :data="systemTemplates" style="width: 100%">
            <el-table-column prop="name" label="名称" width="120" />
            <el-table-column prop="description" label="描述" />
            <el-table-column prop="usageCount" label="使用次数" width="100" align="center" />
            <el-table-column label="操作" width="200" fixed="right">
              <template #default="{ row }">
                <el-button size="small" @click="previewTemplate(row)">预览</el-button>
                <el-button size="small" type="primary" @click="useTemplate(row)">使用</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="我的模板" name="user">
          <el-table :data="userTemplates" style="width: 100%">
            <el-table-column prop="name" label="名称" width="120" />
            <el-table-column prop="description" label="描述" />
            <el-table-column prop="createdAt" label="创建时间" width="180" />
            <el-table-column label="操作" width="250" fixed="right">
              <template #default="{ row }">
                <el-button size="small" @click="editTemplate(row)">编辑</el-button>
                <el-button size="small" type="primary" @click="useTemplate(row)">使用</el-button>
                <el-button size="small" type="danger" @click="deleteTemplate(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- 新建/编辑模板对话框 -->
    <el-dialog
      :title="dialogTitle"
      v-model="dialogVisible"
      width="600px"
      destroy-on-close
    >
      <el-form :model="templateForm" :rules="formRules" ref="formRef" label-width="100px">
        <el-form-item label="模板名称" prop="name">
          <el-input v-model="templateForm.name" placeholder="输入模板名称" />
        </el-form-item>

        <el-form-item label="描述" prop="description">
          <el-input
            v-model="templateForm.description"
            type="textarea"
            :rows="2"
            placeholder="输入模板描述"
          />
        </el-form-item>

        <el-form-item label="系统提示词" prop="systemPrompt">
          <el-input
            v-model="templateForm.systemPrompt"
            type="textarea"
            :rows="4"
            placeholder="输入系统提示词"
          />
        </el-form-item>

        <el-form-item label="用户提示词" prop="userPrompt">
          <el-input
            v-model="templateForm.userPrompt"
            type="textarea"
            :rows="4"
            placeholder="输入用户提示词模板，使用 {{变量}} 表示变量"
          />
          <div class="template-hint">
            可用变量: {{question}}, {{context}}, {{history}}
          </div>
        </el-form-item>

        <el-form-item label="示例" prop="example">
          <el-input
            v-model="templateForm.example"
            type="textarea"
            :rows="3"
            placeholder="输入示例"
          />
        </el-form-item>

        <el-form-item label="标签" prop="tags">
          <el-select
            v-model="templateForm.tags"
            multiple
            filterable
            allow-create
            default-first-option
            placeholder="添加标签"
          >
            <el-option
              v-for="tag in availableTags"
              :key="tag"
              :label="tag"
              :value="tag"
            />
          </el-select>
        </el-form-item>
      </el-form>

      <template #footer>
        <span class="dialog-footer">
          <el-button @click="dialogVisible = false">取消</el-button>
          <el-button type="primary" @click="saveTemplate">保存</el-button>
        </span>
      </template>
    </el-dialog>

    <!-- 预览模板对话框 -->
    <el-dialog title="模板预览" v-model="previewVisible" width="500px">
      <div class="preview-content">
        <h4>系统提示词</h4>
        <pre>{{ previewTemplate.systemPrompt }}</pre>

        <h4>用户提示词模板</h4>
        <pre>{{ previewTemplate.userPrompt }}</pre>

        <h4>示例</h4>
        <pre>{{ previewTemplate.example }}</pre>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'

interface Template {
  id: string
  name: string
  description: string
  systemPrompt: string
  userPrompt: string
  example: string
  tags: string[]
  usageCount: number
  createdAt: string
  isSystem?: boolean
}

// Props
const props = defineProps<{
  characterId?: string
}>()

// Emits
const emit = defineEmits<{
  (e: 'select', template: Template): void
}>()

// 状态
const activeTab = ref('system')
const dialogVisible = ref(false)
const previewVisible = ref(false)
const dialogTitle = computed(() => (editingId.value ? '编辑模板' : '新建模板'))
const editingId = ref<string | null>(null)
const formRef = ref<FormInstance>()

// 模板数据
const systemTemplates = ref<Template[]>([
  {
    id: '1',
    name: '通用助手',
    description: '通用的AI助手模板',
    systemPrompt: '你是一个有帮助的AI助手',
    userPrompt: '问题: {{question}}',
    example: '问题: 你好',
    tags: ['通用'],
    usageCount: 150,
    createdAt: '2024-01-01',
    isSystem: true
  },
  {
    id: '2',
    name: 'RAG问答',
    description: '基于文档的问答模板',
    systemPrompt: '你是一个基于文档的问答助手，请根据提供的上下文回答问题',
    userPrompt: '上下文:\n{{context}}\n\n问题: {{question}}',
    example: '上下文: ...\n问题: ...',
    tags: ['RAG'],
    usageCount: 89,
    createdAt: '2024-01-01',
    isSystem: true
  },
  {
    id: '3',
    name: '代码专家',
    description: '擅长编程问题的模板',
    systemPrompt: '你是一个编程专家，擅长多种编程语言',
    userPrompt: '编程问题: {{question}}',
    example: '编程问题: Python如何读取文件',
    tags: ['编程'],
    usageCount: 67,
    createdAt: '2024-01-01',
    isSystem: true
  }
])

const userTemplates = ref<Template[]>([])

// 表单数据
const templateForm = reactive({
  name: '',
  description: '',
  systemPrompt: '',
  userPrompt: '',
  example: '',
  tags: [] as string[]
})

// 可用标签
const availableTags = ref(['通用', 'RAG', '编程', '翻译', '总结', '创意'])

// 表单验证规则
const formRules: FormRules = {
  name: [
    { required: true, message: '请输入模板名称', trigger: 'blur' },
    { min: 2, max: 50, message: '长度在2-50个字符', trigger: 'blur' }
  ],
  systemPrompt: [
    { required: true, message: '请输入系统提示词', trigger: 'blur' }
  ],
  userPrompt: [
    { required: true, message: '请输入用户提示词模板', trigger: 'blur' }
  ]
}

// 预览模板
const previewTemplate = ref<Template>({
  id: '',
  name: '',
  description: '',
  systemPrompt: '',
  userPrompt: '',
  example: '',
  tags: [],
  usageCount: 0,
  createdAt: ''
})

// 方法
const showNewTemplateDialog = () => {
  editingId.value = null
  templateForm.name = ''
  templateForm.description = ''
  templateForm.systemPrompt = ''
  templateForm.userPrompt = ''
  templateForm.example = ''
  templateForm.tags = []
  dialogVisible.value = true
}

const editTemplate = (template: Template) => {
  editingId.value = template.id
  templateForm.name = template.name
  templateForm.description = template.description
  templateForm.systemPrompt = template.systemPrompt
  templateForm.userPrompt = template.userPrompt
  templateForm.example = template.example
  templateForm.tags = [...template.tags]
  dialogVisible.value = true
}

const saveTemplate = async () => {
  if (!formRef.value) return

  await formRef.value.validate((valid) => {
    if (valid) {
      if (editingId.value) {
        // 编辑现有模板
        const index = userTemplates.value.findIndex(t => t.id === editingId.value)
        if (index !== -1) {
          userTemplates.value[index] = {
            ...userTemplates.value[index],
            ...templateForm,
            id: editingId.value
          }
        }
        ElMessage.success('模板更新成功')
      } else {
        // 创建新模板
        const newTemplate: Template = {
          id: Date.now().toString(),
          ...templateForm,
          usageCount: 0,
          createdAt: new Date().toISOString().split('T')[0]
        }
        userTemplates.value.push(newTemplate)
        ElMessage.success('模板创建成功')
      }

      dialogVisible.value = false
    }
  })
}

const deleteTemplate = (template: Template) => {
  ElMessageBox.confirm('确定删除此模板吗？', '警告', {
    confirmButtonText: '确定',
    cancelButtonText: '取消',
    type: 'warning'
  }).then(() => {
    const index = userTemplates.value.findIndex(t => t.id === template.id)
    if (index !== -1) {
      userTemplates.value.splice(index, 1)
      ElMessage.success('删除成功')
    }
  })
}

const useTemplate = (template: Template) => {
  emit('select', template)
  ElMessage.success(`已应用模板: ${template.name}`)
}

const previewTemplateDialog = (template: Template) => {
  previewTemplate.value = template
  previewVisible.value = true
}

const handleTabClick = () => {
  // 切换标签
}
</script>

<style scoped>
.prompt-templates {
  height: 100%;
}

.templates-card {
  height: 100%;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.template-hint {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
}

.preview-content {
  max-height: 400px;
  overflow-y: auto;
}

.preview-content h4 {
  margin: 16px 0 8px;
  color: #606266;
}

.preview-content pre {
  background-color: #f5f7fa;
  padding: 12px;
  border-radius: 4px;
  white-space: pre-wrap;
  word-wrap: break-word;
  font-family: monospace;
}
</style>
