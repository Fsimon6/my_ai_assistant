# -*- coding: utf-8 -*-
"""
数据库模型
"""
from backend.models.user import User, UserSession
from backend.models.character import AICharacter, Conversation, Text

# 所有模型列表
__all__ = ['User', 'UserSession', 'AICharacter', 'Conversation', 'Text']

# 自动导入所有模型，确保Alembic能发现
from backend.database.base import Base