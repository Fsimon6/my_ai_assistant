<template>
  <div class="dashboard">
<!--    <h1>控制面板</h1>-->
    <h2>欢迎使用 My AI Assistant！</h2>
    <p>这是你的控制面板，未来会展示统计信息和快捷入口</p>

    <div class="stats">
      <el-card class="stat-card">
        <template #header>
          <div class="card-header">
            <span>AI角色</span>
          </div>
        </template>
        <div class="card-content">
          <h3>{{ characters.length }}</h3>
          <p>已创建的AI角色</p>
        </div>
      </el-card>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue"
import { ElMessage } from "element-plus"
import { useCharacterStore } from "@/stores/character"

const characterStore = useCharacterStore()
const characters = characterStore.characters

onMounted(async () => {
  try {
    await characterStore.fetchCharacters()
  } catch (error) {
    ElMessage.error('加载角色数据失败')
  }
})
</script>

<style scoped lang="scss">
.dashboard {
  padding: 20px;

  h1 {
    font-size: 28px;
    margin-bottom: 16px;
    color: #303133;
  }

  p {
    color: #606266;
    margin-bottom: 24px;
  }

  .stats {
    display: flex;
    grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
    gap: 20px;
    margin-top: 30px;

    .stat-card {
      .card-header {
        font-weight: bold;
        color: #303133;
      }

      .card-content {
        h3 {
          font-size: 32px;
          color: #409eff;
          margin: 0 0 8px 0;
        }

        p {
          color: #909399;
          margin: 0;
        }
      }
    }
  }
}
</style>
