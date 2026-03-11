# -*- coding: utf-8 -*-
"""
AI角色数据库模型
"""
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, JSON, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from backend.database.base import Base

class AICharacter(Base):
    """AI角色数据库模型"""
    __tablename__ = 'ai_characters'

    id = Column(Integer, primary_key=True, index=True)
    system_prompt = Column(Text, nullable=False)
    model = Column(String(50), default='gpt-3.5-turbo')
    role_type = Column(String(50), default='assistant')

    # API配置
    api_key = Column(String(255))
    api_provider = Column(String(50), default='openai')

    # 配置选项
    temperature = Column(String(10), default='0.7')
    max_tokens = Column(Integer, default=2000)

    # 状态
    is_active = Column(Boolean, default=True)
    is_public = Column(Boolean, default=False)  # 是否公开

    # 统计信息
    total_conversations = Column(Integer, default=0)
    total_token_used = Column(Integer, default=0)

    # 元数据
    tags = Column(JSON)     # 标签列表
    config = Column(JSON)   # 额外配置

    # 时间戳
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    last_used_at = Column(DateTime(timezone=True))

    def __repr__(self):
        return f'<AICharacter(id={self.id}, name={self.name}, model={self.model})>'

class Conversation(Base):
    """对话记录模型"""
    __tablename__ = 'conversations'

    id = Column(Integer, primary_key=True, index=True)
    character_id = Column(Integer, index=True, nullable=False)
    user_id = Column(Integer, index=True, nullable=False)

    # 对话信息
    title = Column(String(200))     # 对话标题（自动生成）
    texts = Column(JSON, nullable=False)     # 消息列表

    # 统计信息
    text_count = Column(Integer, default=0)
    token_count = Column(Integer, default=0)

    # 状态
    is_active = Column(Boolean, default=True)

    # 时间戳
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    last_used_at = Column(DateTime(timezone=True))

    def __repr__(self):
        return f'<Conversation(id={self.id}, character_id={self.character_id}, title={self.title})>'

class Text(Base):
    """单条消息模型"""
    __tablename__ = 'texts'

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, index=True, nullable=False)

    # 消息内容
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)

    # AI相关
    model = Column(String(50))
    token_used = Column(Integer, default=0)

    # 元数据
    meta_info = Column(JSON)     # 额外元数据

    # 时间戳
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f'<Text(id={self.id}, role={self.role}, content={self.content[:50]}...)>'
