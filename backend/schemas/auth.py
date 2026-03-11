# -*- coding: utf-8 -*-
"""
认证相关的Pydantic模式
"""
from typing import Optional
from pydantic import BaseModel, EmailStr, Field

class Token(BaseModel):
    """令牌响应模式"""
    access_token: str = Field(..., description='访问令牌')
    token_type: str = Field(default='bearer', description='令牌类型')
    expires_in: int = Field(..., description='过期时间（秒）')

class TokenData(BaseModel):
    """令牌数据类型"""
    user_id: Optional[str] = None
    username: Optional[str] = None

class UserBase(BaseModel):
    """用户基础模型"""
    username: str = Field(..., min_length=3, max_length=50, description='用户名')
    email: EmailStr = Field(..., description='邮箱')
    full_name: Optional[str] = Field(None, description='全名')

class UserCreate(UserBase):
    """用户创建模型"""
    password: str = Field(..., min_length=6, description='密码')

class UserLogin(BaseModel):
    """用户登录模型"""
    username: str = Field(..., description='用户名或邮箱')
    password: str = Field(..., description='密码')

class UserResponse(UserBase):
    """用户响应模型"""
    id: int = Field(..., description='用户ID')
    is_active: bool = Field(..., description='是否活跃')
    is_superuser: bool = Field(..., description='是否超级用户')
    created_at: str = Field(..., description='创建时间')

    class Config:
        from_attributes = True

class UserUpdate(BaseModel):
    """用户更新模型"""
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    password: Optional[str] = None
    is_active: Optional[bool] = None

