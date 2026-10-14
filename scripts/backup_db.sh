#!/usr/bin/env bash
# 兼容入口：备份实现已统一到跨平台脚本 scripts/backup_db.py。
#
# 为什么不再在这里实现（旧实现的真实缺陷）：
#   1) 只查 backend/my_ai_assistant.db，而运行时真实库在**仓库根** my_ai_assistant.db
#      -> 数据库实际从未被备份；
#   2) 变量名拼写错误（BACKEND_DIR、缺 $ 的 BACKUP_DIR）在 set -e 下直接中断脚本；
#   3) 完全遗漏 data/excel（Excel 原件 + representation.json，结构化数据的唯一来源）；
#   4) 把 .env 明文复制进 ./backups/（而 backups/ 当时未被 .gitignore 覆盖），存在密钥泄漏风险；
#   5) 依赖 bash，Windows 无法直接运行。
#
# 保留原有调用方式（参数会原样透传）：
#   ./scripts/backup_db.sh                # 备份到 ./backups，保留最近 7 套
#   ./scripts/backup_db.sh --keep 14
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  PY=python
fi
exec "$PY" "$DIR/backup_db.py" "$@"
