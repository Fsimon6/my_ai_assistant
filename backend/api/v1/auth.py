# -*- coding: utf-8 -*-
"""
认证API路由
"""
from urllib.request import Request

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from backend.database.base import get_db
from backend.schemas.auth import UserCreate, UserResponse, Token, UserLogin
from backend.services.auth_service import auth_service
from backend.utils.response import api_response
from backend.utils.auth import get_current_user
from backend.models.user import User

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post("/register", response_model=UserResponse)
async def register(user_data: UserCreate, db: Session = Depends(get_db)):
    """用户注册"""
    try:
        user = await auth_service.create_user(db, user_data)
        return api_response.success(
            data={
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'full_name': user.full_name,
                'is_active': user.is_active,
                'is_superuser': user.is_superuser,
                'created_at': user.created_at.isformat() if user.created_at else None,
            },
            text='注册成功'
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f'注册失败：{str(e)}'
        )


@router.post("/login", response_model=Token)
async def login(form_data: UserLogin, db: Session = Depends(get_db)):
    """用户登录"""
    user = auth_service.authenticate_user(db, form_data.username, form_data.password)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={'WWW-Authenticate': 'Bearer'},
        )

    token_data = auth_service.create_access_token_for_user(user)

    return api_response.success(data=token_data, text='登陆成功')


@router.post("/login/form")
async def login_form(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """OAuth2兼容登录（用于Swagger UI测试）"""
    user = auth_service.authenticate_user(db, form_data.username, form_data.password)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={'WWW-Authenticate': 'Bearer'},
        )

    token_data = auth_service.create_access_token_for_user(user)

    return token_data


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(current_user: User = Depends(get_current_user()), db: Session = Depends(get_db)):
   """获取当前用户信息"""
   if not current_user:
       raise HTTPException(
           status_code=status.HTTP_401_UNAUTHORIZED,
           detail='需要登录'
       )

   return api_response.success(
       data={
           'id': current_user.id,
           'username': current_user.username,
           'email': current_user.email,
           'full_name': current_user.full_name,
           'is_active': current_user.is_active,
           'is_superuser': current_user.is_superuser,
           'created_at': current_user.created_at.isoformat() if current_user.created_at else None,
       },
       text='获取用户信息成功'
   )

@router.post("/logout")
async def logout():
    """用户登出（客户端删除token即可）"""
    return api_response.success(
        data=None,
        text='登出成功'
    )

@router.post("/refresh")
async def refresh_token(current_user: User = Depends(get_current_user())):
    """刷新令牌"""
    if not current_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='需要登录'
        )

    token_data = auth_service.create_access_token_for_user(current_user)

    return api_response.success(
        data=token_data,
        text='令牌刷新成功'
    )

