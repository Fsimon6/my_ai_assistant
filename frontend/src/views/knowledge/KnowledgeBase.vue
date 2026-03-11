<template>
  <div class="knowledge-base">
    <!-- 顶部标题 -->
    <div class="page-header">
      <h1>知识库管理</h1>
      <p>上传和管理您的文档，构建智能知识库</p>
    </div>

    <div class="content-container">
      <!-- 左侧：上传区域 -->
      <div class="upload-section">
        <el-card class="upload-card">
          <template #header>
            <div class="card-header">
              <span class="header-title">上传文档</span>
            </div>
          </template>

          <!-- 上传组件 -->
          <file-uploader @file-uploaded="handleFileUploaded" />

          <!-- 上传历史 -->
          <div class="upload-history" v-if="uploadHistory.length > 0">
            <h3>最近上传</h3>
            <el-timeline>
              <el-timeline-item
                v-for="item in uploadHistory.slice(0, 5)"
                :key="item"
                :timestamp="formatTime(item.timestamp)"
                placement="top"
              >
                <el-card>
                  <div class="history-item">
                    <div class="file-info">
                      <el-icon><Document /></el-icon>
                      <span class="filename">{{ item.filename }}</span>
                    </div>
                    <div class="file-stats">
                      <el-tag size="small" :type="item.status === 'success' ? 'success' : 'danger'">
                        {{ item.status === 'success' ? '成功' : '失败' }}
                      </el-tag>
                      <span class="chunks-count">{{ item.chunks }} chunk</span>
                    </div>
                  </div>
                </el-card>
              </el-timeline-item>
            </el-timeline>
          </div>
        </el-card>
      </div>

      <!-- 右侧：文档列表 -->
      <div class="document-section">
        <el-card class="document-card">
          <template #header>
            <div class="card-header">
              <span class="header-title">文档列表</span>
              <div class="header-actions">
                <el-button type="primary" size="small" @click="refreshDocuments">
                  <el-icon><Refresh /></el-icon>
                  刷新
                </el-button>
              </div>
            </div>
          </template>

          <!-- 操作栏 -->
          <div class="search-bar">
            <el-input
              v-model="searchQuery"
              placeholder="搜索文档..."
              clearable
              @clear="clearSearch"
            >
              <template #prefix>
                <el-icon><Search /></el-icon>
              </template>
            </el-input>
          </div>

          <!-- 文档列表 -->
          <div class="documents-list">
            <el-table
              :data="filteredDocuments"
              style="width: 100%"
              empty-text="暂无文档，请先上传"
            >
              <el-table-column
                prop="filename"
                label="文件名"
                width="250"
              >
                <template #default="scope">
                  <div class="filename-cell">
                    <el-icon class="file-icon">
                      <component :is="getFileIcon(scope.row.type)" />
                    </el-icon>
                    <span class="filename-text">{{ scope.row.filename }}</span>
                  </div>
                </template>
              </el-table-column>

              <el-table-column
                prop="type"
                label="类型"
                width="100"
              >
                <template #default="scope">
                  <el-tag size="small">{{ scope.row.type.toUpperCase() }}</el-tag>
                </template>
              </el-table-column>

              <el-table-column
                prop="size"
                label="大小"
                width="120"
              >
                <template #default="scope">
                  {{ formatFileSize(scope.row.size) }}
                </template>
              </el-table-column>

              <el-table-column
                prop="chunks"
                label="分数段"
                width="100" />

              <el-table-column
                prop="uploadTime"
                label="上传时间"
                width="180" />

              <el-table-column
                label="操作"
                width="200"
                fixed="right">
                <template #default="scope">
                  <div class="action-buttons">
                    <el-button
                      type="primary"
                      size="small"
                      @click="queryDocument(scope.row)"
                    >
                      查询
                    </el-button>
                    <el-button
                      type="info"
                      size="small"
                      @click="previewDocument(scope.row)"
                    >
                      预览
                    </el-button>
                    <el-button
                      type="danger"
                      size="small"
                      @click="deleteDocument(scope.row)"
                    >
                      删除
                    </el-button>
                  </div>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <!-- 统计信息 -->
          <div class="stats-info">
            <el-row :gutter="20">
              <el-col :span="6">
                <el-statistic title="文档总数" :value="documents.length" />
              </el-col>
              <el-col :span="6">
                <el-statistic title="总分段数" :value="totalChunks" />
              </el-col>
              <el-col :span="6">
                <el-statistic title="知识库大小" :value="totalSize" />
              </el-col>
              <el-col :span="6">
                <el-statistic title="最后更新" :value="lastUpdate" />
              </el-col>
            </el-row>
          </div>
        </el-card>

        <!-- 快捷查询 -->
        <el-card class="quick-query-card">
          <template #header>
            <div class="card-header">
              <span class="header-title">快速查询</span>
            </div>
          </template>

          <div class="quick-query-form">
            <el-input
              v-model="quickQuery"
              type="textarea"
              :rows="2"
              placeholder="输入您的问题，快速查询知识库..."
            />
            <div class="query-actions">
              <el-button type="primary" @click="handleQuickQuery">
                查询
              </el-button>
              <el-button
                @click="clearQuickQuery">
                清空
              </el-button>
            </div>

            <!-- 查询结果 -->
            <div v-if="queryResult" class="query-result">
              <h4>查询结果：</h4>
              <div class="result-content">
                {{ queryResult }}
              </div>
            </div>
          </div>
        </el-card>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted} from "vue"
import { ElMessage, ElMessageBox } from "element-plus"
import {
  Document,
  Refeash,
  Search,
  Folder,
  Ticket,
  Picture,
  VideoCamera,
  Files
} from "@element-plus/icons-vue"
import FileUploader from "@/components/chat/FileUploader.vue"
import { ragApi } from '@/services/api'

// 数据
const documents = ref<any[]>([])
const uploadHistory = ref<any[]>([])
const searchQuery = ref('')
const quickQuery = ref('')
const queryResult = ref('')

// 计算属性
const filteredDocuments = computed(() => {
  if (!searchQuery.value) return documents.value
  return documents.value.filter(doc => doc.filename.toLowerCase().includes(searchQuery.value.toLowerCase()))
})

const totalChunks = computed(() => {
  return documents.value.reduce((sum, doc) => sum + (doc.chunks || 0), 0)
})

const totalSize = computed(() => {
  const total = documents.value.reduce((sum, doc) => sum + (doc.size || 0), 0)
  return formatFileSize(total)
})

const lastUpdate = computed(() => {
  if (documents.value.length === 0) return '无'
  const latest = Math.max(...documents.value.map(d => new Data(d.uploadTime).getTime()))
  return new Date(latest).toLocaleDateString()
})

// 文件图标映射
const fileIcons = {
  'pdf': Ticket,
  'txt': Files,
  'docx': Document,
  'doc': Document,
  'md': Files,
  'default': Folder
}

// 生命周期
onMounted(() => {
  loadDocuments()
  loadCollectionInfo()
})

// 方法
const getFileIcon = (fileType: string) => {
  if (fileType.includes('pdf')) return fileIcons.pdf
  if (fileType.includes('text')) return fileIcons.txt
  if (fileType.includes('document')) return fileIcons.docx
  if (fileType.includes('markdown')) return fileIcons.md
  return fileIcons.default
}

const formatFileSize = (bytes: number) => {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i]
}

const formatTime = (timestamp: string) => {
  return new Date(timestamp).toLocaleDateString()
}

const loadDocuments = async () => {
  try {
    // 模拟数据
    documents.value = [
      {
        id: '1',
        filename: '项目需求文档.pdf',
        type: 'pdf',
        size: 2457600,
        chunks: 12,
        uploadTime: '2024-01-10 14:30:00'
      },
      {
        id: '2',
        filename: '技术规范.txt',
        type: 'txt',
        size: 102400,
        chunks: 3,
        uploadTime: '2024-01-09 10:15:00'
      },
      {
        id: '3',
        filename: '用户手册.docx',
        type: 'docx',
        size: 5120000,
        chunks: 25,
        uploadTime: '2024-01-08 16:45:00'
      }
    ]
    ElMessage.success('文档列表加载成功')
  } catch (error) {
    console.error('加载文档失败:', error)
    ElMessage.error('加载文档失败')
  }
}

const loadCollectionInfo = async () => {
  try {
    const info = await ragApi.getCollectionInfo()
    console.log('集合信息：', info)
  } catch (error) {
    console.warn('获取集合信息失败：', error)
  }
}

const handelFileUploaded = async (fileData: any) => {
  try {
    // 调用上传API
    const result = await ragApi.uploadDocument(fileData.file, {
      description: fileData.description
    })

    // 添加到历史记录
    uoloadHistory.valuw.unshift({
      id: Date.now(),
      filename: fileData.name,
      status: 'success',
      chunks: result.total_chunks || 0,
      timestamp: new Date().toISOString()
    })

    // 刷新文档列表
    loadDocuments()

    ElMessage.success(`文件"${fileData.name}" 上传成功`)
  } catch (error) {
    console.error('文件上传失败：', error)

    uploadHistory.value.unshift({
      id: Date.now(),
      filename: fileData.name,
      status: 'error',
      chunks: 0,
      timestamp: new Date().toISOString()
    })

    ElMessage.error('文件上传失败')
  }
}

const handleQuickQuery = async () => {
  if (!quickQuery.value.trim()) {
    ElMessage.warning('请输入查询内容')
    return
  }

  try {
    const response = await ragApi.queryDocument(quickQuery.value, false)
    queryResult.value = response.response || '无相关结果'
    ElMessage.success('查询完成')
  } catch (error) {
    console.error('查询失败：', error)
    queryResult.value = '查询失败，请重试'
    ElMessage.error('查询失败')
  }
}

const queryDocument = (doc: any) => {
  ElMessage.info(`预览文档：${doc.filename} （功能开发中）`)
}

const deleteDocument = async (doc: any) => {
  try {
    await ElMessageBox.confirm(
      `确定要删除文档 "${doc.filename}" 吗?此操作不可撤销。`,
      '确认删除',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )

    // 模拟删除
    documents.value = documents.value.filter(d => d.id !== doc.id)
    ElMessage.success('文档删除成功')
  } catch (error) {
    // 用户取消
  }
}

const refreshDocuments = () => {
  loadDocuments()
  ElMessage.info('文档列表已刷新')
}

const clearSearch = () => {
  searchQuery.value = ''
}

const clearQuickQuery = () => {
  quickQuery.value = ''
  queryResult.value = ''
}
</script>

<style scoped lang="scss">
.knowledge-base {
  padding: 20px;
}

.page-header {
  margin-bottom: 30px;

  h1 {
    font-size: 28px;
    color: var(--text-primary);
    margin-bottom: 8px;
  }

  p {
    color: var(--text-secondary);
    font-size: 16px;
  }
}

.content-container {
  display: grid;
  grid-template-columns: 1fr 2fr;
  gap: 20px;

  @media (max-width: 1200px) {
    grid-template-columns: 1fr;
  }
}

.upload-card,
.document-card,
.quick-query-card {
  margin-bottom: 20px;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;

  .header-title {
    font-size: 18px;
    font-weight: 600;
  }
}

.upload-history {
  margin-top: 20px;
  padding-top: 20px;
  border-top: 1px solid var(--border-light);

  h3 {
    margin-bottom: 15px;
    font-size: 16px;
    color: var(--text-primary);
  }

  .history-item {
    display: flex;
    justify-content: space-between;
    align-items: center;

    .file-info {
      display: flex;
      align-items: center;
      gap: 8px;

      .filename {
        font-weight: 500;
      }
    }

    .file-stats {
      display: flex;
      align-items: center;
      gap: 12px
    }
  }
}

.search-bar {
  margin-bottom: 20px;
}

.documents-list {
  min-height: 300px;

  .filename-cell {
    display: flex;
    align-items: center;
    gap: 8px;

    .file-icon {
      color: var(--primary-color);
      font-size: 18px;
    }

    .filename-text {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
  }

  .action-buttons {
    display: flex;
    gap: 8px;
  }
}

.stats-info {
  margin-top: 20px;
  padding-top: 20px;
  border-top: 1px solid var(--border-light);
}

.quick-query-form {
  .quick-actions {
    display: flex;
    gap: 10px;
    margin-top: 10px;
    margin-bottom: 20px;
  }

  .query-result {
    margin-top: 20px;
    padding: 15px;
    background: var(--bg-base);
    border-radius: 6px;
    border: 1px solid var(--border-light);

    h4 {
      margin-bottom: 10px;
      color: var(--text-primary);
    }

    .result-content {
      line-height: 1.6;
      color: var(--text-regular);
    }
  }
}
</style>
