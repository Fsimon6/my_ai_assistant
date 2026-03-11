# API 文档

## 认证

### 注册
POST /api/v1/auth/register

### 登录
POST /api/v1/auth/login

## 角色管理

### 获取所有角色
GET /api/v1/characters

### 创建角色
POST /api/v1/characters

### 更新角色
PUT /api/v1/characters/{character_id}

### 删除角色
DELETE /api/v1/characters/{character_id}

## 聊天

### 发送消息
POST /api/v1/chat

### 获取聊天历史
GET /api/v1/chat/history

## 知识库管理

### 上传文档
POST /api/v1/knowledge/upload

### 获取文档列表
GET /api/v1/knowledge/documents

### 删除文档
DELETE /api/v1/knowledge/documents/{document_id}
