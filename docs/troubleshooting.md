'''markdown
# 故障排除指南

## 常见问题

### 1. 后端无法启动

**问题**: 'ModuleNotFoundError: No module named 'xxx''
**解决**:
'''bash
cd backend
pip install -r requirements.txt
pip install -r requirements-dev.txt