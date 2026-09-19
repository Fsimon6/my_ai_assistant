# -*- coding: utf-8 -*-
"""
用户模型
"""
from fastapi.openapi.models import Schema
from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey
from sqlalchemy.sql import func
from datetime import datetime
from backend.database.base import Base

class User(Base):
    """用户模型"""
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(100), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(100))
    is_active = Column(Boolean, default=True)
    is_superuser = Column(Boolean, default=False)

    # 时间戳
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f'<User(id={self.id}, username={self.username}, email={self.email})>'

    @property
    def display_name(self):
        """显示名称"""
        return self.full_name or self.username

class UserSession(Base):
    """用户会话模型"""
    __tablename__ = 'user_session'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    session_token = Column(String(255), unique=True, index=True, nullable=False)
    user_agent = Column(String(500))
    ip_address = Column(String(50))

    # 会话状态
    is_active = Column(Boolean, default=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)

    # 时间戳
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_used_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self):
        return f'<UserSession(id={self.id}, user_id={self.user_id})>'
