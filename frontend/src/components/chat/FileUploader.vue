<template>
  <div class="file-uploader">
    <!-- 拖放区域 -->
    <div
      class="drop-zone"
      :class="{ 'is-dragover': isDragOver }"

      @dragover.prevent="handleDragOver"
      @dragleave="handleDragLeave"
      @drop.prevent="handleDrop"
      @click="triggerFileInput"
    >
      <div class="drop-content">
        <el-icon class="upload-icon">
          <UploadFilled/>
        </el-icon>
        <p class="drop-text">
          拖放文件到此处，或<em>点击上传</em>
        </p>
        <p class="drop-hint">
          支持 PDF、TXT、DOCX、MD、XLSX、XLS、CSV、TSV 文件，最大 10MB
        </p>
      </div>

      <input
        ref="fileInput"
        type="file"
        multiple
        :accept="acceptTypes"
        @change="handleFileSelect"
        style="display: none"
      />
    </div>

    <!-- 文件列表 -->
    <div v-if="files.length > 0" class="file-list">
      <div v-for="(file, index) in files" :key="file.id" class="file-item">
        <div class="file-info">
          <el-icon class="file-icon">

            <component :is="getFileIcon(file)"
            />
          </el-icon>
          <div class="file-details">
            <span class="file-name">{{ file.name }}</span>
            <div class="file-meta">
              <span class="file-size">{{ formatFileSize(file.size) }}</span>
              <span class="file-status" :class="file.status">

                {{ getStatusText(file.status) }}
              </span>
            </div>

            <!-- 进度条 -->
            <div v-if="file.status === 'uploading'" class="upload-progress">
              <el-progress
                :percentage="file.progress"
                :stroke-width="4"
                :show-text="false"
              />
            </div>
          </div>
        </div>

        <div class="file-actions">
          <el-button
            v-if="file.status === 'pending' || file.status === 'error'"
            type="text"
            size="small"
            @click="uploadFile(index)"
          >
            上传
          </el-button>

          <el-button
            v-if="file.status === 'uploading'"
            type="text"
            size="small"

            @click="cancelUpload(index)"
          >
            取消
          </el-button>

          <el-button
            v-if="file.status === 'success'"
            type="text"
            size="small"
            @click="previewFile(file)"
          >
            预览
          </el-button>

          <el-button
            type="text"
            size="small"
            @click="removeFile(index)"
            class="remove-btn"
          >
            <el-icon><Close /></el-icon>
          </el-button>
        </div>
      </div>
    </div>

    <!-- 上传按钮 -->
    <div class="upload-actions">
      <el-button
        type="primary"
        :loading="isUploading"
        :disabled="!hasFiles || isUploading"
        @click="uploadAll"
      >
        {{ isUploading ? '上传中...' : `上传文件 (${files.length})` }}
      </el-button>

      <el-button
        v-if="files.length > 0"
        type="text"
        @click="clearFiles"
      >
        清空列表
      </el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from "vue"
import { ElMessage, ElMessageBox } from "element-plus"

import {
  UploadFilled,
  Close,
  Document,
  Picture,
  VideoCamera,
  Files,
  Paperclip,
  Grid
} from "@element-plus/icons-vue";
import * as pdfjsLib from 'pdfjs-dist'

// 定义文件类型
interface UploadFile {
  id: string
  file: File
  name: string
  size: number
  ext: string
  type: string
  status: 'pending' | 'uploading' | 'success' | 'error'
  progress: number
  content?: string
  preview?: string
  error?: string
}

// 支持的MIME类型
const SUPPORTED_TYPES = {
  'application/pdf': 'PDF',
  'text/plain': 'TXT',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'DOCX',
  'application/msword': 'DOC',
  'text/markdown': 'MD',
  'text/html': 'HTML',
  // Excel / 表格（Phase 1A）
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'XLSX',
  'application/vnd.ms-excel': 'XLS',
  'text/csv': 'CSV',
  'text/tab-separated-values': 'TSV'
}

// 表格扩展名（浏览器对 xls/xlsx/tsv 的 MIME 上报不稳定，需按扩展名兜底）
const SPREADSHEET_EXTS = ['xlsx', 'xls', 'csv', 'tsv']

const fileExt = (name: string) => (name.split('.').pop() || '').toLowerCase()
const isSpreadsheet = (ext: string) => SPREADSHEET_EXTS.includes(ext)

// 文件大小限制（10MB）
const MAX_FILE_SIZE = 10 * 1024 * 1024

// 响应式数据
const files = ref<UploadFile[]>([])
const isDragOver = ref(false)
const isUploading = ref(false)
const fileInput = ref<HTMLInputElement>()

// 计算属性
const acceptTypes = computed(() => {
  const exts = SPREADSHEET_EXTS.map(e => `.${e}`)
  return [...Object.keys(SUPPORTED_TYPES), ...exts].join(',')
})
const hasFiles = computed(() => files.value.length > 0)
const pendingFiles = computed(() => files.value.filter(f => f.status === 'pending'))

// 文件图标映射
const fileIcons = {
  'pdf': Document,
  'txt': Document,
  'doc': Document,
  'docx': Document,
  'md': Files,
  'html': Files,
  'xlsx': Grid,
  'xls': Grid,
  'csv': Grid,
  'tsv': Grid,
  'image': Picture,
  'video': VideoCamera,
  'default': Paperclip,
}

// 处理拖放
const handleDragOver = (e: DragEvent) => {
  isDragOver.value = true
  e.preventDefault()
}

const handleDragLeave = () => {
  isDragOver.value = false
}

const handleDrop = (e: DragEvent) => {
  isDragOver.value = false
  e.preventDefault()

  if (e.dataTransfer?.files) {
    addFiles(Array.from(e.dataTransfer.files))
  }
}

// 触发文件选择
const triggerFileInput = () => {
  fileInput.value?.click()
}

// 处理文件选择
const handleFileSelect = (e: Event) => {
  const input = e.target as HTMLInputElement
  if (input.files) {
    addFiles(Array.from(input.files))
    // 清空input，以便选择相同文件
    input.value = ''
  }
}

// 添加文件到列表
const addFiles = (newFiles: File[]) => {
  for (const file of newFiles) {
    // 检查文件大小
    if (file.size > MAX_FILE_SIZE) {
      ElMessage.warning(`文件 "${file.name}" 超过10MB限制`)
      continue
    }

    const ext = fileExt(file.name)
    const knownMime = SUPPORTED_TYPES[file.type as keyof typeof SUPPORTED_TYPES]
    const knownSpreadsheet = isSpreadsheet(ext)

    // 检查文件类型：MIME 或表格扩展名任一命中即可（浏览器 MIME 不可靠）
    if (!knownMime && !knownSpreadsheet) {
      ElMessage.warning(`不支持的文件类型：${file.name}`)
      continue
    }

    // 检查重复文件
    if (files.value.some(f => f.name === file.name && f.size === file.size)) {
      ElMessage.warning(`文件 "${file.name}" 已存在`)
      continue
    }

    files.value.push({
      id: Date.now().toString() + Math.random(),
      file,
      name: file.name,
      size: file.size,
      ext,
      type: file.type,
      status: 'pending',
      progress: 0
    })
  }
}

// 获取文件图标
const getFileIcon = (file: UploadFile) => {
  if (file.ext && (fileIcons as any)[file.ext]) return (fileIcons as any)[file.ext]
  const mimeType = file.type || ''
  if (mimeType.includes('pdf')) return fileIcons.pdf
  if (mimeType.includes('spreadsheet') || mimeType.includes('excel') || mimeType.includes('csv')) return fileIcons.xlsx
  if (mimeType.includes('text')) return fileIcons.txt
  if (mimeType.includes('document')) return fileIcons.doc
  if (mimeType.includes('markdown')) return fileIcons.md
  if (mimeType.includes('image')) return fileIcons.image
  if (mimeType.includes('video')) return fileIcons.video
  return fileIcons.default
}

// 格式化文件大小
const formatFileSize = (bytes: number) => {
  if (bytes === 0) return '0 B'
  const k = 1024
  const size = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + '' + size[i]
}

// 获取文本状态
const getStatusText = (status: UploadFile['status']) => {
  const texts = {
    pending: '等待上传',
    uploading: '上传中',
    success: '上传成功',
    error: '上传失败'
  }
  return texts[status]
}

// 读取文件内容
const readFileContent = async (file: File, asText: boolean = true): Promise<string | ArrayBuffer> => {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()

    reader.onload = (e) => {
      resolve(e.target?.result as any)
    }

    reader.onerror = () => {
      reject(new Error(`读取文件失败：${file.name}`))
    }

    if (file.type === 'application/pdf' && !asText) {
      reader.readAsArrayBuffer(file)
    } else {
      reader.readAsText(file, 'UTF-8')
    }
  })
}

// 提取PDF文本
const extractTextFromPDF = async (arrayBuffer: ArrayBuffer): Promise<string> => {
  try {
    // 配置PDF.js

    pdfjsLib.GlobalWorkerOptions.workerSrc =
      'https://cdn.jsdelivr.net/npm/pdfjs-dist@6.3.289/build/pdf.worker.min.mjs'

    const pdf = await pdfjsLib.getDocument({ data: arrayBuffer }).promise
    let fullText = ''

    for (let i = 1; i <= pdf.numPages; i++) {
      const page = await pdf.getPage(i)
      const textContent = await page.getTextContent()
      const pageText = textContent.items.map((item: any) => item.str).join(' ')
      fullText += pageText + '\n\n'
    }

    return fullText
  } catch (error) {
    throw new Error(`提取PDF文本失败：${error}`)
  }
}

// 上传单个文件
const uploadFile = async (index: number) => {
  const fileItem = files.value[index]

  // 添加空值检查
  if (!fileItem) {
    ElMessage.error('文件不存在')
    return
  }

  try {
    // 更新状态
    fileItem.status = 'uploading'
    fileItem.progress = 0

    // 模拟上传进度
    const progressInterval = setInterval(() => {
      if (fileItem.progress < 90) {
        fileItem.progress += 10
      }
    }, 100)

    // 读取文件内容（表格为结构化数据，不在前端做文本读取）
    let content: string

    if (isSpreadsheet(fileItem.ext)) {
      content = '（表格文件：上传完成后在知识库列表中点击“预览”查看 Sheet 与数据）'
    } else if (fileItem.type === 'application/pdf') {
      const arrayBuffer = await readFileContent(fileItem.file, false) as ArrayBuffer
      content = await extractTextFromPDF(arrayBuffer)
    } else {
      content = await readFileContent(fileItem.file, true) as string
    }

    clearInterval(progressInterval)
    fileItem.progress = 100

    // 稳定性补丁：真正的上传发生在父组件（后端 API）。
    // 这里**不再**提前宣布成功，而是把 done 回调交给父组件，
    // 由父组件根据后端结果回调 done(true/false, message)。
    // 否则后端失败（如 embedding 欠费）时，列表仍会显示"上传成功"，
    // 与"文档列表为空/历史显示失败"自相矛盾。
    const done = (ok: boolean, message?: string) => {
      if (ok) {
        fileItem.status = 'success'
        fileItem.error = undefined
        fileItem.content = content
        ElMessage.success(`文件 "${fileItem.name}" 上传成功`)
      } else {
        fileItem.status = 'error'
        fileItem.error = message || '上传失败'
        ElMessage({
          type: 'error',
          duration: 8000,
          showClose: true,
          message: `文件 "${fileItem.name}" 上传失败：${message || '未知原因'}`
        })
      }
    }

    // 发射上传事件（携带真实 File 对象 + 结果回调，交由父组件调用后端上传 API）
    emit('file-uploaded', {
      file: fileItem.file,
      name: fileItem.name,
      type: fileItem.type,
      size: fileItem.size,
      content: content,
      done
    })

  } catch (error: any) {
    // 确保 fileItem 存在
    if (fileItem) {
      fileItem.status = 'error'
      fileItem.error = error.message
      ElMessage.error(`文件 "${fileItem.name}" 上传失败： ${error.message}`)
    } else {
      ElMessage.error('上传失败，文件不存在')
    }
  }
}

// 上传所有文件
const uploadAll = async () => {
  if (pendingFiles.value.length === 0) {
    ElMessage.info('没有需要上传的文件')
    return
  }

  isUploading.value = true

  try {
    for (let i = 0; i < files.value.length; i++) {
      const fileItem = files.value[i]
      // 添加额外检查
      if (fileItem && fileItem.status === 'pending') {
        await uploadFile(i)
      }
    }

    // 稳定性补丁：只有确实没有任何失败时才提示"全部成功"
    // （真正的结果由父组件通过 done 回调写回 fileItem.status）
    await new Promise(resolve => setTimeout(resolve, 300))
    const failed = files.value.filter(f => f.status === 'error')
    if (failed.length === 0) {
      ElMessage.success('所有文件上传成功')
    } else if (failed.length < files.value.length) {
      ElMessage.warning(`${failed.length} 个文件上传失败，请查看列表中的失败原因`)
    }
  } catch (error) {
    console.error('批量上传失败：', error)
  } finally {
    isUploading.value = false
  }
}

// 取消上传
const cancelUpload = async (index: number) => {  // 添加参数
  const fileItem = files.value[index]

  // 添加空值检查
  if (!fileItem) {
    ElMessage.error('文件不存在')
    return
  }

  if (fileItem.status === 'uploading') {
    // 这里可以实现取消上传的逻辑
    fileItem.status = 'pending'
    fileItem.progress = 0
    ElMessage.info('已取消上传')
  }
}

// 预览文件
const previewFile = (file: UploadFile) => {
  if (isSpreadsheet(file.ext)) {
    ElMessageBox.alert(
      `<div style="line-height:1.8">
        <h4>${file.name}</h4>
        <p>表格文件请前往「知识库管理」列表，点击该文档的「预览」按钮，</p>
        <p>即可查看 Sheet 列表、列名与前若干行数据。</p>
      </div>`,
      '文件预览', {
        dangerouslyUseHTMLString: true,
        confirmButtonText: '知道了'
      }
    )
    return
  }

  const content = file.content?.substring(0, 1000) || '无内容'
  const truncated = file.content && file.content.length > 1000 ? '\n\n...(内容过长，已截断)' : ''

  ElMessageBox.alert(
    `<div style="max-height: 400px; overflow-y: auto;">
      <h4>${file.name}</h4>
      <pre style="white-space: pre-wrap; word-break: break-word; font-family: monospace;">
        ${content}${truncated}
      </pre>
    </div>`,
    '文件预览', {
      dangerouslyUseHTMLString: true,
      customClass: 'file-preview-dialog',
      confirmButtonText: '关闭'
    }
  )
}

// 移除文件
const removeFile = (index: number) => {
  files.value.splice(index, 1)
}

// 清空文件列表
const clearFiles = () => {
  if (files.value.length === 0) return

  ElMessageBox.confirm(
    '确定要清空所有文件吗？',
    '确认清空', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning'
    }
  ).then(() => {
    files.value = []
    ElMessage.success('文件列表已清空')
  })
}

// 事件发射
const emit = defineEmits<{
  'file-uploaded': [data: {
    file: File
    name: string
    type: string
    size: number
    content: string
    // 稳定性补丁：父组件必须回调，用于把"后端真实结果"落到文件状态与提示上
    done: (ok: boolean, message?: string) => void
  }]
}>()
</script>

<style scoped lang="scss">
.file-uploader {
  .drop-zone {
    border: 2px dashed var(--border-light);
    border-radius: 8px;
    padding: 40px 20px;
    text-align: center;
    cursor: pointer;
    transition: all 0.3s ease;
    background: var(--bg-base);

    &:hover {
      border-color: var(--primary-color);
      background: rgba(64, 158, 255, 0.05);
    }

    &.is-dragover {
      border-color: var(--primary-color);
      background: rgba(64, 158, 255, 0.1);
      transform: scale(1.02);
    }

    .drop-content {
      .upload-icon {
        font-size: 48px;
        color: var(--text-placeholder);
        margin-bottom: 16px;
      }

      .drop-text {
        font-size: 16px;
        color: var(--text-primary);
        margin-bottom: 8px;

        em {
          color: var(--primary-color);
          font-style: normal;
          font-weight: 500;
        }
      }

      .drop-hint {
        font-size: 12px;
        color: var(--text-secondary);
      }
    }
  }

  .file-list {
    margin-top: 20px;
    max-height: 300px;
    overflow-y: auto;

    .file-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px;
      border: 1px solid var(--border-light);
      border-radius: 6px;
      margin-bottom: 8px;
      background: white;
      transition: all 0.3s;

      &:hover {
        border-color: var(--primary-color);
        box-shadow: 0 2px 8px rgba(64, 158, 255, 0.1);
      }

      .file-info {
        display: flex;
        align-items: center;
        gap: 12px;
        flex: 1;
        min-width: 0;

        .file-icon {
          font-size: 24px;
          color: var(--primary-color);
          flex-shrink: 0;
        }

        .file-details {
          flex: 1;
          min-width: 0;

          .file-name {
            display: flex;
            font-weight: 500;
            color: var(--text-primary);
            margin-bottom: 4px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
          }

          .file-meta {
            display: flex;
            align-items: center;
            gap: 12px;
            font-size: 12px;

            .file-size {
              color: var(--text-secondary);
            }

            .file-status {
              padding: 1px 6px;
              border-radius: 10px;
              font-size: 10px;

              &.pending {
                background: var(--bg-base);
                color: var(--text-secondary);
              }

              &.uploading {
                background: rgba(103, 194, 58, 0.1);
                color: var(--success-color);
              }

              &.error {
                background: rgba(245, 108, 108, 0.1);
                color: var(--danger-color);
              }
            }
          }

          .upload-progress {
            margin-top: 8px;

            :deep(.el-progress) {
              .el-progress-bar {
                padding-right: 0;
              }
            }
          }
        }
      }

      .file-actions {
        flex-shrink: 0;
        display: flex;
        gap: 4px;

        .remove-btn {
          color: var(--danger-color);

          &.hover {
            color: #f56c6c;
          }
        }
      }
    }
  }

  .upload-actions {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 20px;
    padding-top: 16px;
    border-top: 1px solid var(--border-light);
  }
}

// 预览对话框样式
:deep(.file-preview-dialog) {
  .el-message-box__content {
    max-height: 60vh;
    overflow-y: auto;
  }
}
</style>
