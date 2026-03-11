'''markdown
# 测试指南

## 测试类型

### 单元测试
测试单个函数/组件
'''bash
# 后端单元测试
cd backend
pytest tests/unit/ -v

# 前端单元测试
cd frontend
npm run test:unit