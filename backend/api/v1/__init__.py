# -*- coding: utf-8 -*-
"""
API v1 路由模块
"""
from fastapi import APIRouter

# 导入路由
from .auth import router as auth_router
from .characters import router as characters_router

# 创建主路由器
api_router = APIRouter()

# 包含各个模块的路由
api_router.include_router(auth_router)
api_router.include_router(characters.router)
