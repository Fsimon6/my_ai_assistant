# -*- coding: utf-8 -*-
"""
API v1 路由模块
"""
from fastapi import APIRouter

# 导入路由
from .auth import router as auth_router

# 创建主路由器
api_router = APIRouter()

# 包含各个模块的路由
api_router.include_router(auth_router)

# characters router 依赖 backend.services.character_service ->
# core.advanced_features；其此前扁平导入 from character import 已在
# core/advanced_features.py 改为 from core.character import，现已可解析，
# 故恢复挂载。
from .characters import router as characters_router
api_router.include_router(characters_router)
