<template>
  <div class="upload-progress" v-if="show">
    <div class="progress-overlay" @click="minimize"></div>
    <div class="progress-panel" :class="{ minimized: isMinimized}">
      <!-- 头部 -->
      <div class="progress-header" @click="toggleMinimize">
        <span class="title">
          <el-icon><Upload /></el-icon>
          文件上传 {{ uploadingCount > 0 ? `(${uploadingCount})` : '' }}
        </span>
        <div class="header-actions">
          <el-button
            :icon="isMinimized ? 'Expand' : 'Minus'"
            size="small"
            text
            @click.stop="toggleMinimize" />
          <el-button
            icon="Close"
            size="small"
            text
            @click.stop="close" />
        </div>
      </div>

      <!-- 内容区域 -->
      <div class="progress-content" v-show="!isMinimized">
        <!-- 进行中的上传 -->
        <div class="uploading-list" v-if="uploadingFiles.length > 0">
          <div class="section-title">上传中</div>
          <div v-for="file in uploadingFiles"
               :key="file.id"
               class="file-item"
          >
            <div class="file-info">
              <el-icon><Document /></el-icon>
              <span class="file-name">{{ file.name }}</span>
              <span class="file-size">{{ formatSize(file.size) }}</span>
            </div>
            <div class="progress-bar">
              <el-progress
                :percentage="file.progress"
                :status="file.status === 'error' ? 'exception' : undefined" />
            </div>
            <div class="file-actions">
              <el-button
                v-if="file.status === 'error'"
                type="danger"
                :icon="Refresh"
                circle
                size="small"
                @click="retryUpload(file)" />
              <el-button
                type="info"
                :icon="Close"
                circle
                size="small"
                @click="cancelUpload(file)" />
            </div>
          </div>
        </div>

        <!-- 等待中的上传 -->
        <div class="pending-list" v-if="pendingFiles.length > 0">
          <div class="section-title">等待中 ({{ pendingFiles.length }})</div>
          <div
            v-for="file in pendingFiles"
            :key="file.id"
            class="file-item pending"
          >
            <div class="file-info">
              <el-icon><Timer /></el-icon>
              <span class="file-name">{{ file.name }}</span>
              <span class="file-size">{{ formatSize(file.size) }}</span>
            </div>
            <div class="file-actions">
              <el-button
                type="info"
                :icon="Close"
                circle
                size="small"
                @click="removePending(file)" />
            </div>
          </div>
        </div>

        <!-- 完成的上传 -->
        <div class="completed-list" v-if="completedFiles.length > 0">
          <div class="section-title">已完成 ({{ completedFiles.length }})</div>
          <div
            v-for="file in completedFiles"
            :key="file.id"
            class="file-item completed"
          >
            <div class="file-info">
              <el-icon><SuccessFilled /></el-icon>
              <span class="file-name">{{ file.name }}</span>
              <span class="file-size">{{ formatSize(file.size) }}</span>
            </div>
            <div class="file-actions">
              <el-button
                type="success"
                :icon="View"
                circle
                size="small"
                @click="viewFile(file)" />
            </div>
          </div>
        </div>

        <!-- 空状态 -->
        <div class="empty-state" v-if="totalFiles === 0">
          <el-empty description="暂无上传任务" />
        </div>
      </div>

      <!-- 底部操作栏 -->
      <div class="progress-footer" v-if="isMinimized && totalFiles > 0">
        <div class="stats">
          <span>总计: {{ totalFiles }} 个文件</span>
          <span>已完成: {{ completedCount }}</span>
          <span>总大小: {{ formatTotalSize }}</span>
        </div>
        <div class="actions">
          <el-button
            v-if="hasError"
            type="danger"
            size="small"
            @click="retryAll"
          >
            全部重试
          </el-button>
          <el-button
            v-if="completedFiles.length > 0"
            type="success"
            size="small"
            @click="clearCompleted"
          >
            清除已完成
          </el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import {
  Upload,
  Document,
  Timer,
  SuccessFilled,
  Refresh,
  Close,
  View
} from '@element-plus/icons-vue'

interface UploadFile {
  id: string
  name: string
  size: number
  progress: number
  status: 'uploading' | 'completed' | 'error' | 'pending'
  url?: string
  error?: string
}

// Props
const props = defineProps<{
  files?: UploadFile[]
  maxConcurrent?: number
}>()

// Emits
const emit = defineEmits<{
  (e: 'retry', fileId: string): void
  (e: 'cancel', fileId: string): void
  (e: 'retry-all'): void
  (e: 'clear-completed'): void
  (e: 'view', file: UploadFile): void
}>()

// 状态
const show = ref(true)
const isMinimized = ref(false)

// 文件列表
const uploadingFiles = computed(() => props.files?.filter(f => f.status === 'uploading') || [])
const pendingFiles = computed(() => props.files?.filter(f => f.status === 'pending') || [])
const completedFiles = computed(() => props.files?.filter(f => f.status === 'completed') || [])

const uploadingCount = computed(() => uploadingFiles.value.length)
const totalFiles = computed(() => props.files?.length || 0)
const completedCount = computed(() => completedFiles.value.length)
const hasError = computed(() => props.files?.some(f => f.status === 'error') || false)

// 格式化大小
const formatSize = (bytes: number): string => {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i]
}

const formatTotalSize = computed(() => {
  const total = props.files?.reduce((sum, f) => sum + f.size, 0) || 0
  return formatSize(total)
})

// 方法
const toggleMinimize = () => {
  isMinimized.value = !isMinimized.value
}

const minimize = () => {
  isMinimized.value = true
}

const close = () => {
  show.value = false
}

const retryUpload = (file: UploadFile) => {
  emit('retry', file.id)
}

const cancelUpload = (file: UploadFile) => {
  emit('cancel', file.id)
}

const removePending = (file: UploadFile) => {
  emit('view', file)
}

const retryAll = () => {
  emit('retry-all')
}

const clearCompleted = () => {
  emit('clear-completed')
}

// 监听最小化状态
watch(isMinimized, (val) => {
  if (!val) {
    // 展开时确保位置正确
  }
})
</script>

<style scoped>
.upload-progress {
  position: fixed;
  bottom: 20px;
  right: 20px;
  z-index: 1000
}

.progress-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.3);
  display: none;
}

.progress-panel {
  position: relative;
  width: 400px;
  background: white;
  border-radius: 8px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
  overflow: hidden;
  transition: all 0.3s;
}

.progress-panel.minimized {
  width: 300px;
  height: auto;
}

.progress-panel.minimized .progress-content,
.progress-panel.minimized .progress-footer {
  display: none;
}

.progress-header {
  padding: 12px 16px;
  background: #f5f7fa;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  justify-content: space-between;
  align-items: center;
  cursor: pointer;
}

.progress-header .title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 500;
}

.header-actions {
  display: flex;
  gap: 4px;
}

.progress-content {
  max-height: 400px;
  overflow-y: auto;
  padding: 12px;
}

.section-title {
  font-size: 13px;
  color: #909399;
  margin: 8px 0 4px;
}

.file-item {
  display: flex;
  align-items: center;
  padding: 8px;
  border-radius: 4px;
  margin-bottom: 4px;
  background: #f9f9f9;
}

.file-item.pending {
  opacity: 0.8;
}

.file-item.completed {
  background: #f0f9eb;
}

.file-info {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.file-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}

.file-size {
  font-size: 12px;
  color: #909399;
}

.progress-bar {
  width: 120px;
  margin: 0 12px;
}

.file-actions {
  display: flex;
  gap: 4px;
}

.progress-footer {
  padding: 12px;
  border-top: 1px solid #e4e7ed;
  background: #f9f9f9;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.stats {
  display: flex;
  gap: 12px;
  font-size: 12px;
  color: #606266;
}

.actions {
  display: flex;
  gap: 8px;
}

.empty-state {
  padding: 32px 0;
}

/* 滚动条样式 */
.progress-content::-webkit-scrollbar {
  width: 6px;
}

.progress-content::-webkit-scrollbar-thumb {
  background: #ddd;
  border-radius: 3px;
}
</style>
