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
            <p class="character-model">{{ character?.model || '默认模型' }}</p>
          </div>
        </div>
      </div>
      <div class="toolbar-right">
        <!-- 知识库入口与「清空对话 / 导出」同级、始终可见（不再依赖知识库开关或悬停菜单） -->
        <el-button type="text" @click="showKnowledgeBase" class="menage-btn">
          <el-icon>
            <Folder/>
          </el-icon>
          知识库
        </el-button>
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

          <el-switch
            v-model="enableExcelQuery"
            active-text="表格查询"
            inactive-text="表格查询关"
            @change="toggleExcelQuery"
          />

          <el-tooltip
            v-if="enableExcelQuery"
            content="用自然语言查询已上传的 Excel/CSV（如「列出5店前20条SKU」）。后端会用真实 schema 校验参数，数据由 Python 精确读取，不会让模型编造。"
            placement="top">
            <el-icon class="info-icon"><InfoFilled /></el-icon>
          </el-tooltip>

          <div class="rag-info" v-if="enableRAG">
            <el-tag typr="success" size="small">
              <el-icon><Check /></el-icon>
              知识库已启用
            </el-tag>

            <!-- 知识库入口已上移到工具栏右侧常显（避免同一功能出现两个入口） -->

            <el-tooltip content="当前对话将基于您上传的文档进行智能回答" placeholder="top">
              <el-icon class="info-icon"><InfoFilled /></el-icon>
            </el-tooltip>
          </div>
        </div>
      </div>

    <!-- 对话区域 -->
    <div class="chat-messages" ref="messagesContainer">
      <!-- 历史加载中 -->
      <div v-if="isLoadingHistory" class="history-loading">
        <el-icon class="is-loading"><Loading /></el-icon>
        <span>正在加载历史消息…</span>
      </div>

      <!-- 欢迎消息 -->
      <div v-if="!isLoadingHistory && messages.length === 0" class="welcome-message">
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
                  <!-- 流式阶段：首包到达前显示打字指示；首包后实时显示已累积文本 + 光标（逐 chunk 更新） -->
                  <div v-if="message.isStreaming && !message.content" class="streaming-indicator">
                    <span class="typing-dots">
                      <span></span><span></span><span></span>
                    </span>
                  </div>
                  <div v-else-if="message.isStreaming" class="markdown-content streaming-text">
                    {{ message.content }}<span class="stream-cursor">▋</span>
                  </div>
                  <div v-else class="markdown-content" v-html="renderMarkdown(message.content)">
                  </div>

                  <!-- 表格查询结果（Phase 1C）：数据来自后端 representation.json，前端只负责展示 -->
                  <div v-if="message.excelTable" class="excel-answer">
                    <div class="excel-answer-meta">
                      <el-tag size="small" :type="message.excelTable.continued ? 'warning' : 'success'">
                        {{ message.excelTable.continued ? '继续上一轮' : '表格查询' }}
                      </el-tag>
                      <span v-if="message.excelTable.document">{{ message.excelTable.document }}</span>
                      <span>Sheet：{{ message.excelTable.sheet }}</span>
                      <el-tag v-if="message.excelTable.engine" size="small" type="info">
                        {{ message.excelTable.engine }}
                      </el-tag>
                    </div>
                    <div v-if="message.excelTable.filtersLabel" class="excel-answer-filters">
                      筛选条件：{{ message.excelTable.filtersLabel }}
                    </div>
                    <div v-if="message.excelTable.relaxedLabel" class="excel-answer-filters excel-answer-relaxed">
                      注意：{{ message.excelTable.relaxedLabel }}
                    </div>
                    <!-- Phase 4B：计算字段（由后端解析并渲染 SQL；前端不计算） -->
                    <div v-if="message.excelTable.calcLabel" class="excel-answer-filters excel-answer-calc">
                      <el-tag size="small" type="warning">计算字段</el-tag>
                      {{ message.excelTable.calcLabel }}
                    </div>
                    <div v-if="message.excelTable.calcFilterLabel" class="excel-answer-filters excel-answer-calc">
                      {{ message.excelTable.calcFilterLabel }}
                    </div>
                    <el-table
                      v-if="message.excelTable.data.length"
                      :data="message.excelTable.data"
                      size="small"
                      border
                      max-height="380"
                      style="width: 100%">
                      <el-table-column prop="__row" label="Excel 行" width="90" />
                      <el-table-column
                        v-for="(col, ci) in message.excelTable.columns"
                        :key="ci"
                        :prop="String(ci)"
                        :label="col.name"
                        min-width="140"
                        show-overflow-tooltip>
                        <template #default="scope">
                          <span>{{ scope.row[String(ci)] === null || scope.row[String(ci)] === undefined ? '' : String(scope.row[String(ci)]) }}</span>
                        </template>
                      </el-table-column>
                    </el-table>
                    <div v-else class="excel-answer-note">没有匹配的行（0 条）</div>
                    <div class="excel-answer-note">{{ message.excelTable.rowRangeLabel }}</div>
                  </div>

                  <!-- 统计结果（Phase 3A）：数字 100% 来自 DuckDB，前端只负责展示 -->
                  <div v-if="message.excelAgg" class="excel-agg">
                    <div class="excel-agg-meta">
                      <el-tag size="small" type="success">统计结果</el-tag>
                      <el-tag v-if="message.excelAgg.engine" size="small" type="info">
                        {{ message.excelAgg.engine }}
                      </el-tag>
                      <span v-if="message.excelAgg.inherited" class="excel-agg-inherited">已沿用上一轮条件</span>
                    </div>

                    <div class="excel-agg-value-block">
                      <div class="excel-agg-label">{{ message.excelAgg.valueLabel }}</div>
                      <div class="excel-agg-value">{{ message.excelAgg.valueDisplay }}</div>
                    </div>

                    <div class="excel-agg-rows">
                      <div><span class="k">操作</span><span class="v">{{ message.excelAgg.operationLabel }}</span></div>
                      <div v-if="message.excelAgg.column"><span class="k">目标列</span><span class="v">{{ message.excelAgg.column }}</span></div>
                      <!-- Phase 4B：计算字段（口径由后端给出） -->
                      <div v-if="message.excelAgg.calcLabel"><span class="k">计算字段</span><span class="v">{{ message.excelAgg.calcLabel }}</span></div>
                      <div><span class="k">筛选条件</span><span class="v">{{ message.excelAgg.filtersLabel || '无（全表）' }}</span></div>
                      <div><span class="k">匹配行数</span><span class="v">{{ message.excelAgg.matchedLabel }}</span></div>
                      <div><span class="k">来源</span><span class="v">{{ message.excelAgg.document }}</span></div>
                      <div><span class="k">Sheet</span><span class="v">{{ message.excelAgg.sheet }}</span></div>
                    </div>

                    <div v-if="message.excelAgg.relaxedLabel" class="excel-answer-filters excel-answer-relaxed">
                      注意：{{ message.excelAgg.relaxedLabel }}
                    </div>
                    <div class="excel-answer-note">
                      {{ message.excelAgg.definition }}
                    </div>
                    <div v-if="message.excelAgg.spanLabel" class="excel-answer-note">
                      {{ message.excelAgg.spanLabel }}
                    </div>
                  </div>

                  <!-- 分组统计结果（Phase 3B）：GROUP BY，数值 100% 来自 DuckDB -->
                  <div v-if="message.excelGroupAgg" class="excel-group-agg">
                    <div class="excel-agg-meta">
                      <el-tag size="small" type="primary">分组统计</el-tag>
                      <el-tag v-if="message.excelGroupAgg.engine" size="small" type="info">
                        {{ message.excelGroupAgg.engine }}
                      </el-tag>
                      <span v-if="message.excelGroupAgg.inherited" class="excel-agg-inherited">
                        已沿用上一轮分组
                      </span>
                    </div>

                    <div class="excel-group-meta">
                      <div>
                        <span class="k">分组字段</span>
                        <span class="v">{{ message.excelGroupAgg.groupColumns.join(' + ') }}</span>
                      </div>
                      <div>
                        <span class="k">操作</span>
                        <span class="v">
                          {{ message.excelGroupAgg.operationLabel }}<template
                            v-if="message.excelGroupAgg.column">（{{ message.excelGroupAgg.column }}）</template>
                        </span>
                      </div>
                      <!-- Phase 4B：计算字段（口径由后端给出） -->
                      <div v-if="message.excelGroupAgg.calcLabel">
                        <span class="k">计算字段</span>
                        <span class="v">{{ message.excelGroupAgg.calcLabel }}</span>
                      </div>
                      <div>
                        <span class="k">筛选条件</span>
                        <span class="v">{{ message.excelGroupAgg.filtersLabel || '无（全表）' }}</span>
                      </div>
                      <div v-if="message.excelGroupAgg.sorted">
                        <span class="k">排序</span>
                        <span class="v">{{ message.excelGroupAgg.sortLabel }}</span>
                      </div>
                      <div v-if="message.excelGroupAgg.sorted">
                        <span class="k">TOP-N</span>
                        <span class="v">{{ message.excelGroupAgg.topLabel }}</span>
                      </div>
                      <div>
                        <span class="k">来源</span>
                        <span class="v">{{ message.excelGroupAgg.document }}</span>
                      </div>
                      <div>
                        <span class="k">Sheet</span>
                        <span class="v">{{ message.excelGroupAgg.sheet }}</span>
                      </div>
                    </div>

                    <el-table
                      v-if="message.excelGroupAgg.data.length"
                      :data="message.excelGroupAgg.data"
                      size="small"
                      border
                      max-height="380"
                      style="width: 100%">
                      <!-- 排名序号仅用于展示（顺序完全来自 DuckDB，前端不排序） -->
                      <el-table-column
                        v-if="message.excelGroupAgg.sorted"
                        type="index"
                        label="#"
                        width="60" />
                      <el-table-column
                        v-for="(col, ci) in message.excelGroupAgg.groupColumns"
                        :key="'g' + ci"
                        :prop="'g' + ci"
                        :label="col"
                        min-width="150"
                        show-overflow-tooltip />
                      <el-table-column
                        prop="value"
                        :label="message.excelGroupAgg.valueLabel"
                        min-width="130"
                        align="right" />
                      <el-table-column prop="matched" label="匹配行数" width="100" align="right" />
                    </el-table>
                    <div v-else class="excel-answer-note">
                      没有匹配的数据行，因此没有任何分组（0 个分组）
                    </div>

                    <div class="excel-answer-note">{{ message.excelGroupAgg.summaryLabel }}</div>
                    <div v-if="message.excelGroupAgg.spanLabel" class="excel-answer-note">
                      {{ message.excelGroupAgg.spanLabel }}
                    </div>
                    <div class="excel-answer-note">{{ message.excelGroupAgg.definition }}</div>
                  </div>

                  <!-- 多步分析结果（Phase 4A）：Step 1 分组 + TOP-N → Step 2 对前 N 名再聚合
                       所有数值、顺序、来源说明均由后端给出；前端不计算、不排序、不重新聚合 -->
                  <div v-if="message.excelMultiStep" class="excel-multi-step">
                    <div class="excel-agg-meta">
                      <el-tag size="small" type="warning">多步分析</el-tag>
                      <el-tag v-if="message.excelMultiStep.engine" size="small" type="info">
                        {{ message.excelMultiStep.engine }}
                      </el-tag>
                      <span v-if="message.excelMultiStep.inherited" class="excel-agg-inherited">
                        已沿用上一轮第 1 步
                      </span>
                      <el-tag size="small" type="danger">
                        {{ message.excelMultiStep.stepCountLabel }}
                      </el-tag>
                    </div>

                    <div class="excel-answer-note">分析：{{ message.excelMultiStep.title }}</div>

                    <!-- 第一步 -->
                    <div class="excel-step-block">
                      <div class="excel-step-title">第一步：{{ message.excelMultiStep.step1Title }}</div>
                      <div class="excel-group-meta">
                        <div>
                          <span class="k">分组字段</span>
                          <span class="v">{{ message.excelMultiStep.groupColumns.join(' + ') }}</span>
                        </div>
                        <div>
                          <span class="k">操作</span>
                          <span class="v">
                            {{ message.excelMultiStep.step1.operation_label }}<template
                              v-if="message.excelMultiStep.step1.column">（{{ message.excelMultiStep.step1.column }}）</template>
                          </span>
                        </div>
                        <!-- Phase 4B：计算字段（口径由后端给出） -->
                        <div v-if="message.excelMultiStep.step1CalcLabel">
                          <span class="k">计算字段</span>
                          <span class="v">{{ message.excelMultiStep.step1CalcLabel }}</span>
                        </div>
                        <div>
                          <span class="k">筛选条件</span>
                          <span class="v">{{ message.excelMultiStep.filtersLabel || '无（全表）' }}</span>
                        </div>
                        <div>
                          <span class="k">排序</span>
                          <span class="v">{{ message.excelMultiStep.sortLabel }}</span>
                        </div>
                        <div>
                          <span class="k">TOP-N</span>
                          <span class="v">{{ message.excelMultiStep.topLabel }}</span>
                        </div>
                      </div>

                      <el-table
                        v-if="message.excelMultiStep.data.length"
                        :data="message.excelMultiStep.data"
                        size="small"
                        border
                        max-height="380"
                        style="width: 100%">
                        <!-- 排名序号仅用于展示（顺序完全来自 DuckDB，前端不排序） -->
                        <el-table-column type="index" label="#" width="60" />
                        <el-table-column
                          v-for="(col, ci) in message.excelMultiStep.groupColumns"
                          :key="'mg' + ci"
                          :prop="'g' + ci"
                          :label="col"
                          min-width="180"
                          show-overflow-tooltip />
                        <el-table-column
                          prop="value"
                          :label="message.excelMultiStep.valueLabel"
                          min-width="130"
                          align="right" />
                        <el-table-column prop="matched" label="匹配行数" width="100" align="right" />
                      </el-table>
                      <div v-else class="excel-answer-note">
                        第 1 步没有匹配的数据行（0 个分组），因此第 2 步没有可汇总的输入
                      </div>

                      <div class="excel-answer-note">{{ message.excelMultiStep.step1Summary }}</div>
                      <div class="excel-answer-note">第 1 步来源：{{ message.excelMultiStep.step1.source }}</div>
                    </div>

                    <!-- 第二步 -->
                    <div class="excel-step-block">
                      <div class="excel-step-title">第二步：{{ message.excelMultiStep.step2Title }}</div>
                      <div class="excel-answer-note">
                        数据来源：{{ message.excelMultiStep.step2.source_text }}
                        （输入 {{ message.excelMultiStep.step2.input_rows }} 个分组，
                        其中可数值化 {{ message.excelMultiStep.step2.numeric_rows }} 个）
                      </div>
                      <div class="excel-step2-value">
                        <span class="label">{{ message.excelMultiStep.step2ValueLabel }}</span>
                        <span class="value">{{ message.excelMultiStep.value_display }}</span>
                      </div>
                    </div>

                    <div class="excel-answer-note">来源：{{ message.excelMultiStep.document }}</div>
                    <div class="excel-answer-note">Sheet：{{ message.excelMultiStep.sheet }}</div>
                    <div class="excel-answer-note">{{ message.excelMultiStep.definition }}</div>
                  </div>

                  <!-- 需要澄清：后端明确要求补充信息，模型不编造 -->
                  <div v-if="message.excelClarify" class="excel-clarify">
                    <el-icon><WarningFilled /></el-icon>
                    <div>
                      <div>{{ message.excelClarify }}</div>
                      <div v-if="message.excelCandidates && message.excelCandidates.length" class="excel-candidates">
                        候选：{{ message.excelCandidates.join(' / ') }}
                      </div>
                    </div>
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
  Folder,
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
  ChatLineRound, InfoFilled, WarningFilled
} from "@element-plus/icons-vue";
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { useCharacterStore } from "@/stores/character"
import { getCharacterConversations, clearCharacterConversation } from "@/api/character"
import { ragApi } from "@/services/api"
import type {
  ExcelAggregateResult,
  ExcelGroupedAggregateResult,
  ExcelMultiStepResult,
  ExcelNlQueryResult,
  ExcelQueryResult
} from "@/services/api"
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
// Phase 1C：自然语言表格查询开关（与知识库 RAG 相互独立，可同时存在）
const enableExcelQuery = ref(false)
// Phase 1D：会话标识，用于把「上一轮表格查询上下文」绑定到当前对话
const excelSessionId = ref('')
const newExcelSessionId = () => `sess-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
const ragSources = ref<any[]>([])   // 存储查询来源

// 历史消息加载状态（Stage 21）
const isLoadingHistory = ref(false)
const historyLoadedFor = ref<string | null>(null)  // 已加载历史的角色 id，防止重复加载
const hasUserInteracted = ref(false)               // 用户是否已发送/交互，避免历史覆盖实时消息

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

// 组件挂载时加载数据（含历史恢复，Stage 21）
onMounted(async () => {
  // Phase 1D：本聊天窗口的表格查询会话标识（清空对话时轮换，避免跨会话复用上下文）
  excelSessionId.value = newExcelSessionId()
  await loadCharacter()
  scrollToBottom()

  // 如果角色支持RAG，默认启用
  if (character.value?.supportsRAG) {
    enableRAG.value = true
  }

  // 监听窗口变化
  window.addEventListener('resize', scrollToBottom)
})

onUnmounted(() => {
  window.removeEventListener('resize', scrollToBottom)
})

// 生成欢迎消息（使用已加载的角色名）
const createWelcomeMessage = () => ({
  role: 'assistant',
  content: `你好！我是${character.value?.name || 'AI助手'}，很高兴为您服务！`,
  timestamp: new Date().toISOString()
})

// 加载对话历史（Stage 21：进入聊天页或切换角色后从后端恢复历史）
const loadHistory = async () => {
  // 防重复：同一角色只加载一次；若用户已开始交互，则不再用历史覆盖实时消息
  if (historyLoadedFor.value === characterId.value) return
  if (hasUserInteracted.value) {
    historyLoadedFor.value = characterId.value
    return
  }
  historyLoadedFor.value = characterId.value
  isLoadingHistory.value = true
  try {
    const res = await getCharacterConversations(characterId.value, 100) as any
    const list: any[] = (res?.conversations || [])
    if (list.length > 0) {
      // 映射为 Chat.vue 使用的 message 结构，保持数据库时间顺序（role/content/created_at）
      messages.value = list
        .filter((m: any) => m.role === 'user' || m.role === 'assistant')
        .map((m: any) => ({
          role: m.role,
          content: m.content,
          timestamp: m.created_at || new Date().toISOString()
        }))
    } else {
      // 无历史：显示欢迎语（不重复）
      messages.value = [createWelcomeMessage()]
    }
  } catch (error: any) {
    // 历史加载失败：不白屏，保留欢迎语，提示用户，异常不吞掉
    console.error('加载对话历史失败:', error)
    messages.value = [createWelcomeMessage()]
    const status = error?.response?.status
    if (status !== 401) {
      ElMessage.warning('历史消息加载失败，已开始新对话')
    }
  } finally {
    isLoadingHistory.value = false
    scrollToBottom()
  }
}

// 加载角色信息
const loadCharacter = async () => {
  try {
    await characterStore.fetchCharacter(characterId.value)
    character.value = characterStore.currentCharacter
    // 角色加载成功后再加载历史（JWT 由 requests 拦截器自动携带）
    await loadHistory()
  } catch (error) {
    ElMessage.error('加载角色失败')
    router.push('/characters')
  }
}

// 发送消息
const sendMessage = async () => {
  const message = inputMessage.value.trim()
  if (!message || isLoading.value) return
  hasUserInteracted.value = true

  if (enableExcelQuery.value) {
    // Phase 1C：自然语言表格查询（后端解析意图 + schema 校验 + Python 精确取数）
    await sendExcelQueryMessage(message)
    return
  }

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
  const userMsgIndex = messages.value.length - 1

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

  // 构建多轮历史（不含当前用户消息与占位），注入 query-with-history 维持会话连贯
  const history = messages.value
    .slice(0, userMsgIndex)
    .filter((m: any) => (m.role === 'user' || m.role === 'assistant') && m.content && !m.isStreaming)
    .map((m: any) => ({ role: m.role, content: m.content }))

  try {
    // RAG 流式调用：复用与普通 Chat 相同的 NDJSON 解析器，逐 chunk 渲染；
    // 走 query-with-history 以携带对话历史 + 知识库检索，并保持真正 Streaming。
    const { sendMessage: sendRAGStream } = useStreamingChat({
      url: '/api/v1/rag/query-with-history',
      buildBody: (msg: string) => ({ query: msg, history, stream: true, context_count: 3, character_id: characterId.value }),
      onChunk: (_chunk: string, accumulated: string) => {
        messages.value[aiMessageIndex].content = accumulated
        scrollToBottom()
      },
      onComplete: (fullResponse: string) => {
        messages.value[aiMessageIndex] = {
          role: 'assistant',
          content: fullResponse,
          timestamp: new Date().toISOString(),
          isStreaming: false,
          isRAG: true,
          sources: [],
          fromKnowledgeBase: true
        }
        isLoading.value = false
        scrollToBottom()
      },
      onError: (error: Error) => {
        console.error('RAG流式响应错误:', error)
        messages.value[aiMessageIndex] = {
          role: 'assistant',
          content: '知识库查询失败，请检查网络连接或知识库状态。',
          timestamp: new Date().toISOString(),
          isStreaming: false,
          isRAG: true,
          error: true
        }
        isLoading.value = false
        scrollToBottom()
      }
    })

    await sendRAGStream(message)

  } catch (error) {
    console.error('RAG查询失败：', error)

    messages.value[aiMessageIndex] = {
      role: 'assistant',
      content: '知识库查询失败，请检查网络连接或知识库状态。',
      timestamp: new Date().toISOString(),
      isStreaming: false,
      isRAG: true,
      error: true
    }
  } finally {
    isLoading.value = false
    scrollToBottom()
  }
}

// ======== Phase 1C / 1D：自然语言表格查询 ========
// 后端只返回结构化结果 + Python 生成的摘要；前端负责展示，不做任何数据推理。
// 把后端返回的 applied_filters 渲染成人类可读的筛选条件（不在前端做任何判定）
const OPERATOR_LABELS: Record<string, string> = {
  eq: '=',
  neq: '≠',
  gt: '>',
  gte: '≥',
  lt: '<',
  lte: '≤',
  contains: '包含'
}
const formatFilters = (filters?: any[]): string => {
  if (!filters || !filters.length) return ''
  return filters
    .map((f: any) => {
      const op = OPERATOR_LABELS[f.operator] || f.operator
      const val = f.value === null ? '（空）' : String(f.value)
      return `${f.resolved_column || f.column} ${op} ${val}`
    })
    .join(' 且 ')
}

const buildExcelTable = (r: ExcelQueryResult, documentName: string, continued = false,
                         relaxed: string[] = [], engine = '') => {
  const data = (r.rows || []).map((row: any[], i: number) => {
    const obj: Record<string, any> = { __row: (r.row_excel_numbers || [])[i] ?? '' }
    row.forEach((v: any, ci: number) => { obj[String(ci)] = v })
    return obj
  })
  const nums = r.row_excel_numbers || []
  const seqStart = r.offset + 1
  const seqEnd = r.offset + r.returned_count
  const rowRangeLabel = nums.length
    ? `Excel 行号 ${nums[0]} ~ ${nums[nums.length - 1]}｜共命中 ${r.total_matches} 行，`
      + `本次返回 ${r.returned_count} 行（第 ${seqStart}~${seqEnd} 条，offset=${r.offset}, limit=${r.limit}）`
    : `无匹配行（起点为第 ${seqStart} 条，共命中 ${r.total_matches} 行）`
  // Phase 4B：计算字段口径（标签与说明全部由后端给出）
  const calc = r.calculation || null
  const calcLabel = calc
    ? `${calc.label}（操作：${calc.operation_label}；空值/非数值不参与运算；`
      + `${calc.operation === 'div' ? `「${calc.right_column}」为 0 时结果为空` : '结果为空时不参与比较'}）`
    : ''
  const cf = r.calc_filter || null
  const calcFilterLabel = (cf && calc)
    ? `对计算值筛选：${calc.label} ${CALC_FILTER_SYMBOLS[cf.operator] || cf.operator} ${cf.value}`
      + '（计算值为空的行不匹配任何比较，不会当作 0）'
    : ''
  return {
    columns: r.columns || [],
    data,
    document: documentName,
    sheet: r.sheet_name,
    rowRangeLabel,
    continued,
    // 让用户能直接核对"过滤条件是否正确"
    filtersLabel: formatFilters(r.applied_filters),
    engine: engine || r.engine || '',
    // 条件放宽必须对用户可见（不静默改变语义）
    relaxedLabel: (relaxed && relaxed.length) ? relaxed.join('；') : '',
    calcLabel,
    calcFilterLabel
  }
}

// Phase 4B：计算值比较运算符的展示符号（仅排版，不参与运算）
const CALC_FILTER_SYMBOLS: Record<string, string> = {
  gt: '>', gte: '≥', lt: '<', lte: '≤', eq: '=', neq: '≠'
}

// ======== Phase 3A：统计结果展示 ========
// 数字 100% 来自后端 DuckDB；前端只做标签映射与排版，绝不参与计算。
const AGG_VALUE_LABELS: Record<string, string> = {
  count: '符合条件的行数',
  sum: '合计',
  avg: '平均值',
  min: '最小值',
  max: '最大值'
}

const buildExcelAgg = (a: ExcelAggregateResult, res: ExcelNlQueryResult) => {
  const isCount = a.operation === 'count'
  const calc = a.calculation || null
  const valueLabel = isCount
    ? AGG_VALUE_LABELS.count
    : calc
      ? `${calc.label} ${AGG_VALUE_LABELS[a.operation] || a.operation}`
      : `${a.column || ''} ${AGG_VALUE_LABELS[a.operation] || a.operation}`
  const matchedLabel = isCount
    ? `${a.matched_rows} 行（该 Sheet 共 ${a.total_rows_in_sheet} 行）`
    : `${a.matched_rows} 行，其中可数值化 ${a.numeric_rows} 行`
      + `（空值 ${a.empty_rows}、非数值 ${a.non_numeric_rows} 不计入）`
  const span = a.row_excel_spans
  return {
    engine: res.engine || '',
    inherited: !!res.inherited_from,
    valueLabel,
    valueDisplay: a.value_display,
    operationLabel: a.operation_label || a.operation.toUpperCase(),
    column: a.column || '',
    calcLabel: calc ? `计算字段：${calc.label}（${calc.operation_label}；先逐行计算再聚合）` : '',
    filtersLabel: formatFilters(a.applied_filters || a.filters),
    matchedLabel,
    document: res.document?.filename || '',
    sheet: a.sheet_name,
    definition: a.definition,
    spanLabel: span
      ? `匹配 Excel 行：${span.first} ~ ${span.last}（共 ${a.matched_rows} 行）`
      : '',
    relaxedLabel: (res.relaxed_filters || []).join('；')
  }
}

// ======== Phase 3B：分组统计展示 ========
// 每一组的数值与展示文本均由后端给出；前端不做任何统计与空值判定。
const buildGroupAgg = (g: ExcelGroupedAggregateResult, res: ExcelNlQueryResult) => {
  const isCount = g.operation === 'count'
  const calc = g.calculation || null
  const groupColumns = (g.group_by || []).map(x => x.name)
  const valueLabel = isCount
    ? '行数'
    : calc
      ? `${calc.label} ${AGG_VALUE_LABELS[g.operation] || g.operation}`
      : `${g.column || ''} ${AGG_VALUE_LABELS[g.operation] || g.operation}`
  const data = (g.rows || []).map((r, i) => {
    const obj: Record<string, any> = {
      __i: i,
      value: r.value_display,
      matched: r.matched_rows
    }
    groupColumns.forEach((_, ci) => {
      obj['g' + ci] = (r.group_display && r.group_display[ci] !== undefined)
        ? r.group_display[ci]
        : ''
    })
    return obj
  })
  const span = g.row_excel_spans
  // Phase 3C：排序与 TOP-N 信息全部来自后端（前端只展示，绝不排序）
  const sortLabel = g.sorted
    ? `${g.order_by_label} ${g.order_dir === 'desc' ? '↓ 从高到低' : '↑ 从低到高'}`
    : '未排序'
  const topLabel = g.top_n ? `前 ${g.top_n} 个（共 ${g.total_groups} 个分组）` : '未截取'
  const summaryLabel = g.sorted
    ? `共 ${g.total_groups} 个分组`
      + (g.truncated_by_top_n ? ` → 返回前 ${g.returned_groups} 个` : `，本次全部返回`)
      + `｜${g.sort_description}`
      + `｜匹配 ${g.matched_rows} 行（该 Sheet 共 ${g.total_rows_in_sheet} 行）`
    : `共 ${g.total_groups} 个分组｜匹配 ${g.matched_rows} 行`
      + `（该 Sheet 共 ${g.total_rows_in_sheet} 行）｜未做排序`
  return {
    engine: res.engine || '',
    inherited: !!res.inherited_from,
    groupColumns,
    valueLabel,
    operationLabel: g.operation_label || g.operation.toUpperCase(),
    column: g.column || '',
    calcLabel: calc ? `${calc.label}（${calc.operation_label}；先逐行计算再聚合）` : '',
    filtersLabel: formatFilters(g.applied_filters || g.filters),
    document: res.document?.filename || '',
    sheet: g.sheet_name,
    data,
    sortLabel,
    topLabel,
    sorted: !!g.sorted,
    summaryLabel,
    spanLabel: span ? `匹配 Excel 行区间：${span.first} ~ ${span.last}` : '',
    definition: g.definition
  }
}

// ======== Phase 4A：多步分析展示 ========
// Step 1 的每一行与 Step 2 的最终值都直接取自后端；前端只做字符串拼装与展示。
const buildMultiStep = (m: ExcelMultiStepResult, res: ExcelNlQueryResult) => {
  const s1 = m.step1
  const s2 = m.step2
  const isCount1 = s1.operation === 'count'
  const s1Calc = s1.calculation || null
  const groupColumns = (s1.group_by || []).map(x => x.name)
  const valueLabel = isCount1
    ? '行数'
    : s1Calc
      ? `${s1Calc.label} ${AGG_VALUE_LABELS[s1.operation] || s1.operation}`
      : `${s1.column || ''} ${AGG_VALUE_LABELS[s1.operation] || s1.operation}`

  const data = (s1.rows || []).map((r, i) => {
    const obj: Record<string, any> = {
      __i: i,
      value: r.value_display,
      matched: r.matched_rows
    }
    groupColumns.forEach((_, ci) => {
      obj['g' + ci] = (r.group_display && r.group_display[ci] !== undefined)
        ? r.group_display[ci]
        : ''
    })
    return obj
  })

  const sortLabel = s1.sorted
    ? `${s1.order_by_label} ${s1.order_dir === 'desc' ? '↓ 从高到低' : '↑ 从低到高'}`
    : '未排序'
  const topLabel = s1.top_n ? `前 ${s1.top_n} 个（共 ${s1.total_groups} 个分组）` : '未截取'
  const step1Summary = `共 ${s1.total_groups} 个分组`
    + (s1.truncated_by_top_n ? ` → 第 1 步取前 ${s1.returned_groups} 个` : `，本次全部返回`)
    + `｜匹配 ${s1.matched_rows} 行（该 Sheet 共 ${s1.total_rows_in_sheet} 行）`

  const op1 = s1.operation_label || s1.operation.toUpperCase()
  const op2 = s2.operation_label || s2.operation.toUpperCase()
  const step2ValueLabel = s2.operation === 'count'
    ? '第二步结果（分组个数）'
    : `第二步结果（${op2}）`

  return {
    engine: m.engine || res.engine || '',
    inherited: res.inherited_from === 'analysis',
    stepCountLabel: `共 ${m.step_count} 步（上限 ${m.max_steps}）`,
    step1: s1,
    step2: s2,
    groupColumns,
    valueLabel,
    data,
    filtersLabel: formatFilters(s1.applied_filters || s1.filters),
    sortLabel,
    topLabel,
    step1Summary,
    document: res.document?.filename || m.filename || '',
    sheet: s1.sheet_name || m.sheet_name,
    step2ValueLabel,
    value_display: m.value_display,
    title: `先按「${groupColumns.join(' + ')}」分组${s1.sorted ? `并按${sortLabel}` : ''}`
      + `${s1.top_n ? `取前 ${s1.top_n} 名` : '得到全部分组'}，`
      + `再对第 1 步结果（${s2.input_rows} 个分组）的${valueLabel}执行 ${op2}`,
    step1CalcLabel: s1Calc ? `${s1Calc.label}（${s1Calc.operation_label}；先逐行计算再聚合）` : '',
    step1Title: `按「${groupColumns.join(' + ')}」分组并 ${op1}`
      + (s1Calc ? `（计算字段 ${s1Calc.label}）` : (s1.column ? `（${s1.column}）` : ''))
      + (s1.sorted ? `，${sortLabel}，${topLabel}` : ''),
    step2Title: `对第 1 步的 ${s2.input_rows} 个分组的${valueLabel}再执行 ${op2}`,
    definition: m.definition
  }
}

const sendExcelQueryMessage = async (message: string) => {
  messages.value.push({
    role: 'user',
    content: message,
    timestamp: new Date().toISOString(),
    isExcelQuery: true
  })
  const userIndex = messages.value.length - 1
  const aiIndex = messages.value.length
  messages.value.push({
    role: 'assistant',
    content: '',
    isStreaming: true,
    isExcelQuery: true,
    timestamp: new Date().toISOString()
  })

  inputMessage.value = ''
  isLoading.value = true
  scrollToBottom()

  try {
    const res: ExcelNlQueryResult = await ragApi.nlQueryExcel(message, characterId.value, excelSessionId.value)

    if (res.status === 'not_excel') {
      // 不是表格查询：回收本轮占位，透明回退到原有链路（保持普通文档问答不受影响）
      messages.value.splice(userIndex, 2)
      isLoading.value = false
      if (enableRAG.value) {
        await sendRAGMessage(message)
      } else {
        await sendNormalMessage(message)
      }
      return
    }

    if (res.status === 'ok' && res.multi_step) {
      // Phase 4A：两步分析（Step 1 分组 + TOP-N → Step 2 对前 N 名再聚合）
      messages.value[aiIndex] = {
        role: 'assistant',
        content: res.message || '多步分析完成',
        timestamp: new Date().toISOString(),
        isStreaming: false,
        isExcelQuery: true,
        excelMultiStep: buildMultiStep(res.multi_step, res),
        sources: []
      }
    } else if (res.status === 'ok' && res.group_aggregate) {
      // Phase 3B：分组统计表格（数字来自 DuckDB，非 LLM）
      messages.value[aiIndex] = {
        role: 'assistant',
        content: res.message || '分组统计完成',
        timestamp: new Date().toISOString(),
        isStreaming: false,
        isExcelQuery: true,
        excelGroupAgg: buildGroupAgg(res.group_aggregate, res),
        sources: []
      }
    } else if (res.status === 'ok' && res.aggregate) {
      // Phase 3A：统计结果卡片（数字来自 DuckDB，非 LLM）
      messages.value[aiIndex] = {
        role: 'assistant',
        content: res.message || '统计完成',
        timestamp: new Date().toISOString(),
        isStreaming: false,
        isExcelQuery: true,
        excelAgg: buildExcelAgg(res.aggregate, res),
        sources: []
      }
    } else if (res.status === 'ok' && res.result) {
      messages.value[aiIndex] = {
        role: 'assistant',
        content: res.message || '查询完成',
        timestamp: new Date().toISOString(),
        isStreaming: false,
        isExcelQuery: true,
        excelTable: buildExcelTable(res.result, res.document?.filename || '', !!res.continued,
                                    res.relaxed_filters || [], res.engine || ''),
        sources: []
      }
    } else if (res.status === 'clarify') {
      messages.value[aiIndex] = {
        role: 'assistant',
        content: '',
        timestamp: new Date().toISOString(),
        isStreaming: false,
        isExcelQuery: true,
        excelClarify: res.message || '需要更多信息才能查询，请补充。',
        excelCandidates: res.candidates || []
      }
    } else {
      messages.value[aiIndex] = {
        role: 'assistant',
        content: '表格查询失败：' + (res.message || '未知错误'),
        timestamp: new Date().toISOString(),
        isStreaming: false,
        isExcelQuery: true,
        error: true
      }
    }
  } catch (error: any) {
    const detail = error?.response?.data?.detail
    const text = typeof detail === 'string' ? detail : (detail?.message || error?.message || '网络错误')
    messages.value[aiIndex] = {
      role: 'assistant',
      content: '表格查询失败：' + text,
      timestamp: new Date().toISOString(),
      isStreaming: false,
      isExcelQuery: true,
      error: true
    }
  } finally {
    isLoading.value = false
    scrollToBottom()
  }
}

const toggleExcelQuery = (enabled: boolean) => {
  enableExcelQuery.value = enabled
  if (enabled) {
    ElMessage.success('已启用表格查询：可用自然语言查询已上传的 Excel/CSV')
  } else {
    ElMessage.info('已关闭表格查询')
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

// 切换角色（/chat/:id 参数变化，组件不重新挂载）：清理旧角色内存消息并重新加载
watch(characterId, async (newId: string, oldId: string) => {
  if (newId === oldId) return
  messages.value = [createWelcomeMessage()]
  isLoadingHistory.value = false
  historyLoadedFor.value = null
  hasUserInteracted.value = false
  inputMessage.value = ''
  await loadCharacter()
})

// 发送快速问题
const sendQuickQuestion = (question: string) => {
  inputMessage.value = question
  sendMessage()
}

// 清空对话（前端内存 + 后端持久化，Stage 23）
const clearConversation = async () => {
  // 1. 弹出确认
  try {
    await ElMessageBox.confirm(
      '确定要清空当前对话吗？此操作不可撤销，将删除所有聊天记录。',
      '确认清空',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )
  } catch {
    return  // 用户取消，不执行任何清空
  }

  try {
    // 2. 调用后端 DELETE，删除成功后再清理前端状态
    await clearCharacterConversation(characterId.value)
    messages.value = [createWelcomeMessage()]
    historyLoadedFor.value = null
    hasUserInteracted.value = false
    inputMessage.value = ''
    // Phase 1D：轮换表格查询会话标识 -> 旧的分页上下文不再被复用
    excelSessionId.value = newExcelSessionId()
    ElMessage.success('对话已清空')
  } catch (error: any) {
    // 后端删除失败：保持现有历史，不假装清空成功
    if (error?.response?.status !== 401) {
      ElMessage.error('清空对话失败，请稍后重试')
    }
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

  // 添加流式占位符
  messages.value.push({
    role: 'assistant',
    content: '',
    isStreaming: true,
    timestamp: new Date().toISOString()
  })
  const aiMessageIndex = messages.value.length - 1

  isLoading.value = true
  scrollToBottom()

  try {
    // 复用普通对话流式链路（真实 LLM + 逐 chunk）；服务侧自动加载多轮历史
    const { sendMessage: sendStreaming } = useStreamingChat({
      characterId: characterId.value,
      onChunk: (_chunk: string, accumulated: string) => {
        messages.value[aiMessageIndex].content = accumulated
        scrollToBottom()
      },
      onComplete: (fullResponse: string) => {
        messages.value[aiMessageIndex] = {
          role: 'assistant',
          content: fullResponse,
          timestamp: new Date().toISOString(),
          isStreaming: false
        }
        isLoading.value = false
        scrollToBottom()
      },
      onError: (error: Error) => {
        console.error('重新生成流式响应错误:', error)
        messages.value.splice(aiMessageIndex, 1)
        ElMessage.error('重新生成失败：' + error.message)
        isLoading.value = false
      }
    })
    await sendStreaming(userMessage)
  } catch (error: any) {
    console.error('重新生成失败：', error)
    messages.value.splice(aiMessageIndex, 1)
    ElMessage.error('重新生成失败')
    isLoading.value = false
  }
}

// 导出对话（纯前端下载为 .txt，标注发言角色与时间）
const exportConversation = () => {
  if (messages.value.length === 0) {
    ElMessage.warning('当前没有对话内容')
    return
  }
  const conversationText = messages.value
    .filter(m => !m.isStreaming && m.content)
    .map(m => {
      const role = m.role === 'user' ? '用户' : 'AI助手'
      const time = m.timestamp ? ` [${new Date(m.timestamp).toLocaleString()}]` : ''
      return `${role}${time}：\n${m.content}`
    })
    .join('\n\n')

  const blob = new Blob([conversationText], { type: 'text/plain;charset=utf-8' })
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
  // 该路径只是把文件内容插入输入框（不走后端上传），因此直接回报成功
  fileData.done?.(true)
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
    // 多了一个常显入口（知识库）后，窄屏时允许换行并保持右对齐，
    // 避免按钮溢出/重叠或产生横向滚动条
    flex-wrap: wrap;
    justify-content: flex-end;
    row-gap: 4px;
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

  // 流式输出期间：保留换行/空格，实时呈现已累积文本
  .streaming-text {
    white-space: pre-wrap;
    word-break: break-word;
  }

  // 流式光标（闪烁）
  .stream-cursor {
    display: inline-block;
    margin-left: 2px;
    font-weight: bold;
    color: var(--text-secondary, #909399);
    animation: stream-blink 1s step-start infinite;
  }

  @keyframes stream-blink {
    50% {
      opacity: 0;
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

.history-loading {
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
    // 极窄屏：工具栏整体可换行，避免入口挤压聊天区
    flex-wrap: wrap;
    row-gap: 8px;

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

// Phase 1C：表格查询结果
.excel-answer {
  margin-top: 10px;

  .excel-answer-meta {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 8px;
    font-size: 13px;
    color: var(--text-secondary);
  }

  // Phase 2：筛选条件回显（供用户核对过滤条件）
  .excel-answer-filters {
    margin-bottom: 8px;
    padding: 6px 10px;
    border-radius: 6px;
    background: rgba(64, 158, 255, 0.08);
    border-left: 3px solid var(--el-color-primary, #409eff);
    font-size: 13px;
    color: var(--text-primary, #303133);
    word-break: break-all;
  }

  // Phase 2：条件放宽提示（必须对用户可见，不静默改变语义）
  .excel-answer-relaxed {
    background: rgba(230, 162, 60, 0.12);
    border-left-color: var(--el-color-warning, #e6a23c);
  }

  .excel-answer-note {
    margin-top: 6px;
    font-size: 12px;
    color: var(--text-secondary);
  }
}

// Phase 3A：统计结果卡片
.excel-agg {
  margin-top: 10px;
  padding: 14px 16px;
  border: 1px solid var(--el-color-success-light-7, #b3e19d);
  border-radius: 8px;
  background: var(--el-color-success-light-9, #f0f9eb);

  .excel-agg-meta {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
    font-size: 13px;
    color: var(--text-secondary);
  }

  .excel-agg-inherited {
    font-size: 12px;
    color: var(--el-color-warning, #e6a23c);
  }

  .excel-agg-value-block {
    padding: 6px 0 12px;
    border-bottom: 1px dashed var(--el-color-success-light-5, #95d475);

    .excel-agg-label {
      font-size: 13px;
      color: var(--text-secondary);
    }

    .excel-agg-value {
      margin-top: 2px;
      font-size: 30px;
      font-weight: 700;
      line-height: 1.2;
      color: var(--el-color-success-dark-2, #529b2e);
      word-break: break-all;
    }
  }

  .excel-agg-rows {
    margin-top: 10px;
    display: grid;
    gap: 4px;
    font-size: 13px;

    > div {
      display: flex;
      gap: 8px;
    }

    .k {
      flex: 0 0 72px;
      color: var(--text-secondary);
    }

    .v {
      flex: 1;
      word-break: break-all;
      color: var(--text-primary, #303133);
    }
  }
}

// Phase 3B：分组统计卡片（与 Phase 3A 单值卡片、普通表格查询在视觉上明确区分）
.excel-group-agg {
  margin-top: 10px;
  padding: 14px 16px;
  border: 1px solid var(--el-color-primary-light-7, #a0cfff);
  border-left: 4px solid var(--el-color-primary, #409eff);
  border-radius: 8px;
  background: var(--el-color-primary-light-9, #ecf5ff);

  .excel-agg-meta {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
    font-size: 13px;
    color: var(--text-secondary);
  }

  .excel-agg-inherited {
    font-size: 12px;
    color: var(--el-color-warning, #e6a23c);
  }

  .excel-group-meta {
    margin-bottom: 10px;
    display: grid;
    gap: 4px;
    font-size: 13px;

    > div {
      display: flex;
      gap: 8px;
    }

    .k {
      flex: 0 0 72px;
      color: var(--text-secondary);
    }

    .v {
      flex: 1;
      word-break: break-all;
      color: var(--text-primary, #303133);
    }
  }
}

// Phase 4B：计算字段提示（行级/统计/分组/多步共用同一视觉语言）
.excel-answer-calc {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #b88230;
}

.excel-clarify {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-top: 8px;
  padding: 10px 12px;
  border-radius: 6px;
  background: rgba(230, 162, 60, 0.1);
  border: 1px solid rgba(230, 162, 60, 0.35);
  color: #b88230;
  font-size: 13px;
  line-height: 1.6;

  .excel-candidates {
    margin-top: 4px;
    color: var(--text-secondary);
  }
}

// Phase 4A：多步分析卡片（Step 1 明细 + Step 2 汇总，与 Phase 3 卡片在视觉上明确区分）
.excel-multi-step {
  margin-top: 10px;
  padding: 14px 16px;
  border: 1px solid var(--el-color-warning-light-7, #f3d19e);
  border-left: 4px solid var(--el-color-warning, #e6a23c);
  border-radius: 8px;
  background: var(--el-color-warning-light-9, #fdf6ec);

  .excel-agg-meta {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
    font-size: 13px;
    color: var(--text-secondary);
  }

  .excel-agg-inherited {
    font-size: 12px;
    color: var(--el-color-warning, #e6a23c);
  }

  .excel-step-block {
    margin-top: 10px;
    padding: 12px 14px;
    border-radius: 6px;
    background: rgba(255, 255, 255, 0.72);
    border: 1px dashed var(--el-color-warning-light-5, #eebe77);
  }

  .excel-step-title {
    margin-bottom: 8px;
    font-size: 13px;
    font-weight: 600;
    color: var(--el-color-warning-dark-2, #b88230);
  }

  .excel-step2-value {
    display: flex;
    align-items: baseline;
    gap: 12px;
    margin-top: 8px;

    .label {
      font-size: 13px;
      color: var(--text-secondary);
    }

    .value {
      font-size: 26px;
      font-weight: 700;
      color: var(--el-color-warning-dark-2, #b88230);
      word-break: break-all;
    }
  }

  .excel-group-meta {
    margin-bottom: 10px;
    display: grid;
    gap: 4px;
    font-size: 13px;

    > div {
      display: flex;
      gap: 8px;
    }

    .k {
      flex: 0 0 72px;
      color: var(--text-secondary);
    }

    .v {
      flex: 1;
      word-break: break-all;
      color: var(--text-primary, #303133);
    }
  }
}
</style>
