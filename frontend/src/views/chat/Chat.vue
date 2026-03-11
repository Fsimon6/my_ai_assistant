<template>
  <div class="chat-container">
    <!-- 顶部工具栏 -->
    <div class="chat-toolbar">
      <div class="toolbar-left">
        <el-button type="text" @click="goBack">
          <el-icon>
            <ArrowLeft/>
          </el-icon>

          返回
        </el-button>
        <div class="character-info">
          <span class="character-icon">🤖</span>
          <div>
            <h3>{{ character?.name || 'AI助手' }}</h3>
            <p class="character-model">{{ character?.model || 'gpt-3.5-turbo ' }}</p>
          </div>
        </div>
      </div>
      <div class="toolbar-right">
        <el-button type="text" @click="clearConversation">
          <el-icon>
            <Delete/>
          </el-icon>
          清空对话
        </el-button>
        <el-button type="text" @click="exportConversation">
          <el-icon>
            <Download/>
          </el-icon>
          导出
        </el-button>
        <el-dropdown @command="handleToolCommand">
          <el-button type="text">
            <el-icon>
              <More/>
            </el-icon>
            更多
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="copy">复制对话</el-dropdown-item>
              <el-dropdown-item command="save">保存模板</el-dropdown-item>
              <el-dropdown-item command="settings">对话设置</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </div>

    <!-- RAG开关 -->
      <div class="rag-controls" v-if="character?.supportsRAG !== false">
        <div class="rag-controls-inner">
          <el-switch
            v-model="enableRAG"
            active-text="启用知识库"
            inactive-text="关闭知识库"
            @change="toggleRAG"
          />

          <div class="rag-info" v-if="enableRAG">
            <el-tag typr="success" size="small">
              <el-icon><Check /></el-icon>
              知识库已启用
            </el-tag>

            <el-button
              type="text"
              size="small"
              @click="showKnowledgeBase"
              class="menage-btn"
            >
              <el-idcon><Folder /></el-idcon>
              管理知识库
            </el-button>

            <el-tooltip content="当前对话将基于您上传的文档进行智能回答" placeholder="top">
              <el-icon class="info-icon"><InfoFilled /></el-icon>
            </el-tooltip>
          </div>
        </div>
      </div>

    <!-- 对话区域 -->
    <div class="chat-messages" ref="messagesContainer">
      <!-- 欢迎消息 -->
      <div v-if="messages.length === 0" class="welcome-message">
        <div class="welcome-icon"></div>
        <h2>开始与{{ character?.name || 'AI助手' }}对话</h2>
        <p>输入您的问题，{{ character?.name || 'AI助手' }}将为您提供帮助</p>

        <div class="quick-questions">
          <h4>快速提问：</h4>
          <div class="quick-chips">
            <el-tag
              v-for="(question, index) in quickQuestions"
              :key="index"
              class="question-chips"

              @click="sendQuickQuestion(question)"
            >
              {{ question }}
            </el-tag>
          </div>
        </div>
      </div>

      <!-- 消息列表 -->
      <div v-else class="messages-list">
        <div
            v-for="(message, index) in messages"
            :key="index"
            :class="['message-item', message.role]"
          >
            <div class="message-avatar">
              <span v-if="message.role === 'user'">👤</span>
              <span v-else>🤖</span>
            </div>
          <div class="message-content">
              <div class="message-header">
                <span class="sender">
                  {{ message.role === 'user' ? '你' : character?.name || 'AI助手' }}</span>
                <span class="timestamp">{{ formatTime(message.timestamp) }}</span>
              </div>
            <div class="message-body">
                <!-- 用户消息 -->
                <div v-if="message.role === 'user'" class="user-message">
                  {{ message.content }}
                </div>

              <!-- AI消息 -->
              <div v-else class="ai-message">
                  <div v-if="message.isStreaming" class="streaming-indicator">
                    <span class="typing-dots">
                      <span></span><span></span><span></span>
                    </span>
                </div>
                <div v-else class="markdown-content" v-html="renderMarkdown(message.content)">
                </div>

                <!-- 消息操作 -->
                <div class="message-actions">
                  <el-button
                    type="text"
                    size="small"

                    @click="copyMessage(message.content)"
                  >
                    <el-icon><CopyDocument /></el-icon>
                    复制
                  </el-button>
                  <el-button
                    type="text"
                    size="small"

                    @click="regenerateMessage(index)"
                  >
                    <el-icon><Refresh /></el-icon>
                    重新生成
                  </el-button>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- 加载指示器 -->
        <div v-if="isLoading" class="loading-indicator">
          <el-icon class="is-loading"><Loading /></el-icon>
          <span>AI正在思考...</span>
        </div>
      </div>
    </div>

    <!-- 输入区域 -->
    <div class="chat-input-area">
      <div class="input-tools">
        <el-tooltip content="上传文件" placement="top">
          <el-button type="text"
                     @click="toggleFileUpload">
            <el-icon><Paperclip /></el-icon>
          </el-button>
        </el-tooltip>

        <el-tooltip content="表情符号" placement="top">
          <el-button type="text"
                     @click="showPromptTemplates">
            <el-icon><MagicStick /></el-icon>
          </el-button>
        </el-tooltip>
      </div>

      <!-- 文件上传区域 -->
      <div v-if="showFileUpload"
           class="file-upload-area">
        <FileUploader @file-uploaded="handleFileUploaded" />
      </div>

      <!-- 输入框 -->
      <div class="input-wrapper">
        <el-input
          v-model="inputMessage"
          type="textarea"
          :rows="3"
          :maxlength="2000"
          placeholder="输入消息...（Shift+Enter换行，Enter发送）"

          @keydown.enter.exact.prevent="sendMessage"

          @keydown.shift.enter.exact.prevent="inputMessage += '\n'"
          resize="none"
          :disabled="isLoading"
        />
        <div class="input-actions">
          <span class="char-count">{{ inputMessage.length }}/2000</span>
          <el-button
            type="primary"
            :loading="isLoading"
            :disabled="!inputMessage.trim()"
            @click="sendMessage"
          >
            <template #loading>
              <el-icon class="is-loading"><Loading /></el-icon>
              发送中
            </template>
            <template #default>
              <el-icon><Promotion /></el-icon>
              发送
            </template>
          </el-button>
        </div>
      </div>

      <!-- 快捷操作 -->
      <div class="quick-actions">
        <el-button
          v-for="action in quickActions"
          :key="action.label"
          size="small"
          :type="action.type"
          plain
          @click="action.handler"
        >
          <el-icon v-if="action.icon"><component :is="action.icon" /></el-icon>
          {{ action.label }}
        </el-button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick, watch} from "vue"
import { useRoute, useRouter } from "vue-router"
import { ElMessage, ElMessageBox } from "element-plus";
import {
  ArrowLeft,
  Delete,
  More,
  CopyDocument,
  Download,
  Paperclip,
  Star,
  View,
  Edit,
  Loading,
  MagicStick,
  Promotion,
  Refresh,
  ChatLineRound, InfoFilled
} from "@element-plus/icons-vue";
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { useCharacterStore } from "@/stores/character"
import FileUploader from '@/components/chat/FileUploader.vue'
import type { Character, SpeakResponse } from "@/types/character"
import { useStreamingChat } from '@/composables/useStreamingChat'

const route = useRoute()
const router = useRouter()
const characterStore = useCharacterStore()

// 路由参数
const characterId = computed(() => route.params.id as string)
const character = ref<Character | null>(null)
const messages = ref<any[]>([
  // 可以添加初始欢迎消息
  {
    role:'assistant',
    content: `你好！我是${character.value?.name || 'AI助手'}，很高兴为您服务！`,
    timestamp: new Date().toISOString()
  }
])

// 在loadCharacter 成功后更新
if (messages.value[0]?.role === 'assistant') {
  messages.value[0].content = `你好！我是${character.value?.name || 'AI助手'}，很高兴为您服务！`
}
const inputMessage = ref('')
const isLoading = ref(false)
const showFileUpload = ref(false)
const enableRAG = ref(false)
const ragSources = ref<any[]>([])   // 存储查询来源

// DOM引用
const messagesContainer = ref<HTMLElement>()

// 快速问题示例
const quickQuestions = ref([
  '帮我写一个Python函数',
  '解释一下什么是RAG',
  '如何优化数据库查询？',
  '写一个关于人工智能的简短故事'
])

// 快捷操作
const quickActions = computed(() => [
  {
    label: '优化表达',
    type: 'primary',
    icon: ChatLineRound,
    handler: () => optimizeExpression(),
  },
  {
    label: '总结对话',
    type: 'success',
    icon: View,
    handler: () => summarizeConversation()
  },
  {
    label: '翻译成英文',
    type: 'warning',
    icon: Edit,
    handler: () => translateToEnglish()
  }
])

// 组件挂载时加载数据
onMounted(async () => {
  await loadCharacter()
  scrollToBottom()

  // 监听窗口变化
  window.addEventListener('resize', scrollToBottom)
})

onUnmounted(() => {
  window.removeEventListener('resize', scrollToBottom)
})

// 加载角色信息
const loadCharacter = async () => {
  try {
    await characterStore.fetchCharacter(characterId.value)
    character.value = characterStore.currentCharacter
  } catch (error) {
    ElMessage.error('加载角色失败')
    router.push('/characters')
  }
}

// 发送消息
const sendMessage = async () => {
  const message = inputMessage.value.trim()
  if (!message || isLoading.value) return

  if (enableRAG.value) {
    // 使用RAG查询
    await sendRAGMessage(message)
  } else {
    // 使用普通对话
    await sendNormalMessage(message)
  }
}

// 普通发送方法
const sendNormalMessage = async (message: string) => {
  // 添加到消息列表
  const userMessage = {
    role: 'user',
    content: message,
    timestamp: new Date().toISOString()
  }
  messages.value.push(userMessage)

  // 添加AI回复占位符
  const aiMessageIndex = messages.value.length
  messages.value.push({
    role: 'assistant',
    content: '',
    isStreaming: true,
    timestamp: new Date().toISOString()
  })

  inputMessage.value = ''
  isLoading.value = true
  scrollToBottom()

  try {
    // 根据设置选择使用流式还是普通API
    const useStream = true // 可以从设置中获取这个值

    if (useStream) {
      // 流式调用
      const { sendMessage: sendStreaming } = useStreamingChat({
        characterId: characterId.value,
        onChunk: (_chunk, accumulated) => {
          messages.value[aiMessageIndex].content = accumulated
          scrollToBottom()
        },
        onComplete: (fullResponse) => {
          messages.value[aiMessageIndex] = {
            role: 'assistant',
            content: fullResponse,
            timestamp: new Date().toISOString(),
            isStreaming: false
          }
          isLoading.value = false
          scrollToBottom()
        },
        onError: (error) => {
          console.error('流式响应错误:', error)
          messages.value.splice(aiMessageIndex, 1)
          ElMessage.error('请求失败: ' + error.message)
          isLoading.value = false
        }
      })

      await sendStreaming(message)
    } else {
      // 普通API调用
      const response = await characterStore.speakToCharacter(
        characterId.value,
        message
      )

      messages.value[aiMessageIndex] = {
        role: 'assistant',
        content: response.response,
        timestamp: response.timestamp,
        isStreaming: false
      }
      isLoading.value = false
      scrollToBottom()
    }
  } catch (error) {
    console.error('发送消息失败:', error)
    messages.value.splice(aiMessageIndex, 1)
    ElMessage.error('发送消息失败，请重试')
    isLoading.value = false
  }
}

// RAG发送方法
const sendRAGMessage = async (message: string) => {
  const userMessage = {
    role: 'user',
    content: message,
    timestamp: new Date().toISOString(),
    isRAG: true
  }
  messages.value.push(userMessage)

  // 添加AI回复占位符
  const aiMessageIndex = messages.value.length
  messages.value.push({
    role: 'assistant',
    content: '',
    isStreaming: true,
    isRAG: true,
    timestamp: new Date().toISOString(),
    sources: []   // 初始化来源数组
  })

  inputMessage.value = ''
  isLoading.value = true
  scrollToBottom()

  try {
    // 调用RAG查询API
    const response = await ragApi.queryDocument(message, false)

    // 更新AI消息
    message.value[aiMessageIndex] = {
      role: 'assistant',
      content: response.response || "未找到相关文档",
      timestamp: new Date().toISOString(),
      isRAG: true,
      sources: response.sources || [],   // 如果有来源信息
      fromKnowledgeBase: true
    }

    // 存储来源信息
    ragSources.value = response.sources || []

  } catch (error) {
    console.error('RAG查询失败：', error)

    messages.value[aiMessageIndex] = {
      role: 'assistant',
      content: '知识库查询失败，请检查网络连接或知识库状态。',
      timestamp: new Data().toISOString(),
      isRAG: true,
      error: true
    }
  } finally {
    isLoading.value = false
    scrollToBottom()
  }
}

// ========添加RAG控制方法========
const toggleRAG = (enabled: boolean) => {
  enableRAG.value = enabled
  if (enabled) {
    ElMessage.success('已启用知识库，对话将基于您的文档内容')
  } else {
    ElMessage.info('已关闭知识库，使用普通对话模式')
  }
}

const showKnowledgeBase = () => {
  // 跳转到知识库管理页面
  router.push('/knowledge')
}

// 在组件挂载时检查角色是否支持RAG
onMounted(async () => {
  await loadCharacter()
  scrollToBottom()

  // 如果角色支持RAG，默认启用
  if (character.value?.supportsRAG) {
    enableRAG.value = true
  }

  window.addEventListener('resize', scrollToBottom)
})

// 发送快速问题
const sendQuickQuestion = (question: string) => {
  inputMessage.value = question
  sendMessage()
}

// 清空对话
const clearConversation = async () => {
  try {
    await ElMessageBox.confirm(
      '确定要清空当前对话吗？此操作不可撤销。',
      '确认清空',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )

    messages.value = []
    ElMessage.success('对话已清空')
  } catch (error) {
    // 用户取消
  }
}

// 复制消息
const copyMessage = async (content: string) => {
  try {
    await navigator.clipboard.writeText(content)
    ElMessage.success('已复制到剪贴板')
  } catch (error) {
    ElMessage.error('复制失败')
  }
}

// 重新生成消息
const regenerateMessage = async (index: number) => {
  // 获取用户的上一条消息
  const userMessageIndex = index - 1
  if (userMessageIndex < 0 || messages.value[userMessageIndex].role !== 'user') {
    ElMessage.warning('无法重新生成此消息')
    return
  }

  const userMessage = messages.value[userMessageIndex].content

  // 移除当前AI回复
  messages.value.splice(index, 1)

  // 添加新的AI回复占位符
  messages.value.push({
    role: 'assistant',
    content: '',
    isStreaming: true,
    timestamp: new Date().toISOString()
  })

  isLoading.value = true
  scrollToBottom()

  try {
    const response = await characterStore.speakToCharacter(
      characterId.value,
      userMessage
    )

    // 更新AI消息
    const lastIndex = messages.value.length - 1
    messages.value[lastIndex] = {
      role: 'assistant',
      content: response.response,
      timestamp: response.timestamp,
      isStreaming: false
    }
  } catch (error) {
    console.error('重新生成失败：', error)
    messages.value.pop()  // 移除失败的占位符
    ElMessage.error('重新生成失败')
  } finally {
    isLoading.value = false
    scrollToBottom()
  }
}

// 导出对话
const exportConversation = () => {
  const conversationText = messages.value.map(msg => `${msg.content}`).join('\n\n')

  const blob = new Blob([conversationText], { type: 'text/plain' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `对话记录_${new Date().toISOString().split('T')[0]}.txt`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)

  ElMessage.success('对话已导出')
}

// 工具命令处理
const handleToolCommand = (command: string) => {
  switch (command) {
    case 'copy':
      exportConversation()
      break
    case 'save':
      ElMessage.info('保存模板功能开发中')
      break
    case 'settings':
      ElMessage.info('对话设置功能开发中')
      break
  }
}

// 优化表达
const optimizeExpression = () => {
  inputMessage.value = `请优化这段文字的表达：${inputMessage.value}`
}

// 总结对话
const summarizeConversation = () => {
  if (messages.value.length === 0) {
    ElMessage.warning('当前没有对话内容')
    return
  }

  const conversationText = messages.value
    .slice(-5)
    .map(msg => `${msg.role === 'user' ? '用户' : 'AI'}: ${msg.content}`)
    .join('\n')

  inputMessage.value = `请总结以下对话内容：\n\n${conversationText}`
}

// 翻译成英文
const translateToEnglish = () => {
  if (!inputMessage.value.trim()) {
    ElMessage.warning('请输入要翻译的内容')
    return
  }

  inputMessage.value = `请将以下内容翻译成英文：${inputMessage.value}`
}

// 处理文件上传
const handleFileUploaded = (fileData: any) => {
  inputMessage.value += `[文件：${fileData.name}]\n${fileData.content}`
  showFileUpload.value = false
  ElMessage.success('文件已上传')
}

// 切换文件上传显示
const toggleFileUpload = () => {
  showFileUpload.value = !showFileUpload.value
}

// 切换表情选择器
const toggleEmojiPicker = () => {
  ElMessage.info('表情选择器功能开发中')
}

// 显示提示词模板
const showPromptTemplates = () => {
  ElMessage.info('提示词模板功能开发中')
}

// 返回上一页
const goBack = () => {
  router.push('/characters')
}

// 滚动到底部
const scrollToBottom = () => {
  nextTick(() => {
    if (messagesContainer.value) {
      messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight
    }
  })
}

// 格式化时间
const formatTime = (timestamp: string) => {
  const date = new Date(timestamp)
  return date.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit'
  })
}

// 渲染Markdown
const renderMarkdown = (content: string) => {
  const rawHtml = marked.parse(content) as string
  return DOMPurify.sanitize(rawHtml)
}

// 监听消息变化，自动滚动
watch(messages, () => {
  scrollToBottom()
}, {deep: true})
</script>

<style scoped lang="scss">
.chat-container {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: linear-gradient(135deg, #f5f7fa 0%, #e4e7ed 100%);
}

.chat-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 20px;
  background: white;
  border-bottom: 1px solid var(--border-light);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.05);

  .toolbar-left {
    display: flex;
    align-items: center;
    gap: 20px;

    .character-info {
      display: flex;
      align-items: center;
      gap: 12px;

      .character-icon {
        font-size: 24px;
      }

      h3 {
        margin: 0;
        font-size: 16px;
        font-weight: 600;
        color: var(--text-primary);
      }

      .character-model {
        margin: 2px 0 0;
        font-size: 12px;
        color: var(--text-secondary);
        background: var(--bg-base);
        padding: 2px 8px;
        border-radius: 10px;
        display: inline-block;
      }
    }
  }

  .toolbar-right {
    display: flex;
    align-items: center;
    gap: 8px;
  }
}

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
  background: var(--bg-base);

  // 自定义滚动条
  &::-webkit-scrollbar {
    width: 6px;
  }

  &::-webkit-scrollbar-track {
    background: transparent;
  }

  &::-webkit-scrollbar-thumb {
    background: var(--border-base);
    border-radius: 3px;

    &:hover {
      background: var(--text-placeholder);
    }
  }
}

.welcome-message {
  text-align: center;
  padding: 60px 20px;
  max-width: 600px;
  margin: 0 auto;

  .welcome-icon {
    font-size: 64px;
    margin-bottom: 20px;
  }

  h3 {
    font-size: 24px;
    color: var(--text-primary);
    margin-bottom: 12px;
  }

  p {
    color: var(--text-secondary);
    margin-bottom: 40px;
    font-size: 16px;
  }

  .quick-questions {
    h4 {
      margin-bottom: 16px;
      color: var(--text-regular);
    }

    .question-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: center;

      .question-chip {
        cursor: pointer;
        transition: all 0.3s;

        &:hover {
          background: var(--primary-color);
          color: white;
          transform: translateY(-2px);
        }
      }
    }
  }
}

.messages-list {
  max-width: 800px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.message-item {
  display: flex;
  gap: 12px;
  animation: fadeIn 0.3s ease;

  &.user {
    flex-direction: row-reverse;

    .message-content {
      align-items: flex-end;

      .message-header {
        flex-direction: row-reverse;
      }

      .user-message {
        background: linear-gradient(135deg, #409eff 0%, #67c23a 100%);
        color: white;
        border-radius: 18px 18px 4px 18px;
      }
    }
  }

  &.assistant {
    .ai-message {
      background: white;
      border-radius: 18px 18px 18px 4px;
      border: 1px solid var(--border-light);
    }
  }

  .message-avatar {
    width: 36px;
    height: 36px;
    border-radius: 50%;
    background: white;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 20px;
    flex-shrink: 0;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  }

  .message-content {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 8px;
    max-width: 70%;
  }

  .message-header {
    display: flex;
    align-items: center;
    gap: 8px;

    .sender {
      font-size: 14px;
      font-weight: 500;
      color: var(--text-primary);
    }

    .timestamp {
      font-size: 12px;
      color: var(--text-secondary);
    }
  }

  .message-body {
   .user-message,
   .ai-message {
     padding: 12px 16px;
     line-height: 1.6;
     word-break: break-word;
   }

    .user-message {
      background: var(--primary-color);
      color: white;
    }

    .ai-message {
      .streaming-indicator {
        padding: 12px 16px;

        .typing-dots {
          display: inline-flex;
          gap: 4px;

          span {
            width: 8px;
            height: 8px;
            background: var(--text-secondary);
            border-radius: 50%;
            animation: typing 1.4s infinite ease-in-out;

            &:nth-child(2) {
              animation-delay: 0.2s;
            }

            &:nth-child(3) {
              animation-delay: 0.4s;
            }
          }
        }
      }

      .markdown-content {
        padding: 12px 16px;

        :deep(*) {
          margin: 8px 0;

          &:first-child {
            margin-top: 0;
          }

          &:last-child {
            margin-bottom: 0;
          }
        }

        :deep(code) {
          background: var(--bg-base);
          padding: 2px 6px;
          border-radius: 4px;
          font-family: 'Consolas', monospace;
          font-size: 14px;
        }

        :deep(pre) {
          background: var(--bg-base);
          padding: 12px;
          border-radius: 8px;
          overflow-x: auto;
          margin: 12px 0;

          code {
            background: transparent;
            padding: 0;
          }
        }

        :deep(blockquote) {
          border-left: 4px solid var(--border-light);
          padding-left: 12px;
          color: var(--text-secondary);
          margin-left: 0;
        }
      }

      .message-actions {
        padding: 8px 16px 12px;
        border-top: 1px solid var(--border-lighter);
        display: flex;
        gap: 8px;

        :deep(.el-button) {
          padding: 2px 8px;
          font-size: 12px;
          height: auto;
        }
      }
    }
  }
}

.loading-indicator {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 16px;
  color: var(--text-secondary);

  .el-icon {
    font-size: 18px;
  }
}

.chat-input-area {
  background: white;
  border-top: 1px solid var(--border-light);
  padding: 16px 20px;
  box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.05);

  .input-tools {
    display: flex;
    gap: 8px;
    margin-bottom: 12px;

    :deep(.el-button) {
      padding: 8px;
    }
  }

  .file-upload-area {
    margin-bottom: 12px;
    border: 2px dashed var(--border-light);
    border-radius: 8px;
    padding: 16px;
    background: var(--bg-base);
  }

  .input-wrapper {
    .input-actions {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 8px;

      .char-count {
        font-size: 12px;
        color: var(--text-secondary);
      }

      :deep(.el-button) {
        height: 36px;
        padding: 0 20px;
      }
    }
  }

  .quick-actions {
    display: flex;
    gap: 8px;
    margin-top: 12px;
    justify-content: center;

    :deep(.el-button) {
      font-size: 12px;
      height: 28px;
      padding: 0 12px;
    }
  }
}

// 动画
@keyframes fadeIn {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@keyframes typing {
  0%, 60%, 100% {
    transform: translateY(0);
  }
  30% {
    transform: translateY(-6px);
  }
}

// 响应式设计
@media (max-width: 768px) {
  .chat-toolbar {
    padding: 8px 12px;

    .toolbar-left {
      gap: 12px;

      .character-info {
        h3 {
          font-size: 14px;
        }

        .character-model {
          font-size: 10px;
        }
      }
    }
  }

  .chat-messages {
    padding: 12px;
  }

  .welcome-message {
    padding: 40px 12px;

    h2 {
      font-size: 20px;
    }

    .question-chips {
      .question-chip {
        font-size: 12px;
      }
    }
  }

  .message-item {
    .message-content {
      max-width: 85%;
    }
  }

  .chat-input-area {
    padding: 12px;
  }
}

//
</style>
