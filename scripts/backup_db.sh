#！/bin/bash
# 数据库备份脚本

set -e

# 配置
BACKUP_DIR="./backups"
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_NAME="ai_assistant_backup_${DATE}"
RETENTION_DAYS=7

# 颜色定义
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo " 开始数据库备份..."

# 创建备份目录
mkdir -p $BACKUP_DIR

# 备份SQLite数据库
if [ -f "backend/my_ai_assistant.db" ]; then
  echo " 备份SQLite数据库..."
  cp backend/my_ai_assistant.db "$BACKUP_DIR/${BACKUP_NAME}.db"

  # 创建压缩副本
  tar -czf "BACKEND_DIR/${BACKUP_NAME}_db.tar.gz" backend/my_ai_assistant.db
  echo " SQLite数据库备份完成：${BACKUP_NAME}.db"
fi

# 备份向量数据库
if [ -d "data/chroma_db" ]; then
  echo " 备份向量数据库..."
  tar -czf "BACKUP_DIR/${BACKUP_NAME}_chroma.tar.gz" data/chroma_db
  echo " 向量数据库备份完成：${BACKUP_NAME}_chroma.tar.gz"
fi

# 备份上传的文件
if [ -d "data/uploads" ]; then
  echo " 备份上传文件..."
  tar -czf "$BACKUP_DIR/${BACKUP_NAME}_uploads.tar.gz" data/uploads
  echo " 上传文件备份完成：${BACKUP_NAME}_uploads.tar.gz"
fi

# 备份配置文件
echo " 备份配置文件..."
CONFIG_FILES=(
  "backend/.env"
  "backend/config.py"
  "docker-compose.yml"
  ".env"
)

for config_file in "${CONFIG_FILES[@]}"; do
  if [ -f "$config_file" ]; then
    cp "$config_file" "$BACKUP_DIR/${BACKUP_NAME}_$(basename $config_file)"
  fi
done

# 创建备份清单
cat > "$BACKUP_DIR/${BACKUP_NAME}_manifest.json" << EOF
{
  "backup_name": "${BACKUP_NAME}",
  "date": "${DATE}",
  "files": [
    {
      "name": "${BACKUP_NAME}.db",
      "type": "sqlite_database",
      "size": "$(du -h "$BACKUP_DIR/${BACKUP_NAME}.db" 2>/dev/null | cut -f1 || echo "N/A")"
    },
    {
      "name": "${BACKUP_NAME}_chroma.tar.gz",
      "type": "vector_database",
      "size": "$(du -h "$BACKUP_DIR/${BACKUP_NAME}_chroma.tar.gz" 2>/dev/null | cut -f1 || echo "N/A")"
    },
    {
      "name": "${BACKUP_NAME}_uploads.tar.gz",
      "type": "uploads_files",
      "size": "$(du -h "$BACKUP_DIR/${BACKUP_NAME}_uploads.tar.gz" 2>/dev/null | cut -f1 || echo "N/A")"
    }
  ],
  "system_info": {
    "hostname": "$(hostname)",
    "user": "$(whoami)",
    "timestamp": "$(date)"
  }
}
EOF

echo " 备份清单已创建"

# 清理旧备份
echo " 清理旧备份（保留最近${RETENTION_DAYS}天）..."
find $BACKUP_DIR -name "ai_assistant_backup_*" -type f -mtime+$RETENTION_DAYS -delete

# 显示备份统计
echo ""
echo " 备份统计："
echo "=============================="
echo "备份目录：$BACKUP_DIR"
echo "备份名称：$BACKUP_NAME"
echo "备份时间：$(date)"
echo ""
echo "备份文件列表："
ls -lh $BACKUP_DIR/${BACKUP_NAME}*
echo ""
echo "磁盘使用情况："
du -sh $BACKUP_DIR
echo ""

# 创建恢复脚本
cat > "$BACKUP_DIR/${BACKUP_NAME}_restore.sh" << 'EOF'
#!/bin/bash
# 数据库恢复脚本

set -e

BACKUP_NAME="$1"

if [ -z "$BACKUP_NAME" ]; then
  echo "使用方法：$0 <备份名称>"
  echo "示例：$0 ai_assistant_backup_20240101_120000"
  exit 1
fi

echo "开始恢复数据库备份：$BACKUP_NAME"

# 停止服务（如果正在运行）
echo "停止相关服务..."
docker-compose down 2>/dev/null || true

# 恢复SQLite数据库
if [ -f "$BACKUP_NAME}.db" ]; then
  echo "恢复SQLite数据库..."
  cp "${BACKUP_NAME}.db" backend/my_ai_assistant.db
fi

# 恢复向量数据库
if [ -f "${BACKUP_NAME}_chroma.tar.gz" ]; then
  echo "恢复向量数据库..."
  rm -rf data/chroma_db
  tar -xzf "${BACKUP_NAME}_chroma.tar.gz" -C .
fi

# 恢复上传文件
if [ -f "${BACKUP_NAME}_uploads.tar.gz" ]; then
  echo "恢复上传文件..."
  rm -rf data/uploads
  tar -xzf "${BACKUP_NAME}_uploads.tar.gz" -C .
fi

echo "恢复完成！"
echo "启动服务：docker-compose up -d"
EOF

chmod +x "$BACKUP_DIR/${BACKUP_NAME}_restore.sh"

echo -e "${GREEN} 数据库备份完成！${NC}"
echo ""
echo " 恢复方法："
echo " cd $BACKUP_DIR"
echo " ./${BACKUP_NAME}_restore.sh ${BACKUP_NAME}"
echo ""
echo " 下次自动备份：可以添加到crontab"
echo " 0 2 * * * /path/to/backup_db.sh"