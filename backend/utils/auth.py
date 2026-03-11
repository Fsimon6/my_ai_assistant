"""认证依赖和工具"""
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt
from sqlalchemy.orm import Session

from backend.utils.security import verify_token
from backend.database.base import get_db
from backend.models.user import User

# OAuth2密码承载令牌方案
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/api/v1/auth/login",
    auto_error=False    # 允许匿名访问
)

async def get_current_user(token: Optional[str] = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    """
    获取当前用户依赖

    如果未提供token，返回Noen（允许匿名访问）
    如果token无效，抛出401错误
    """
    if not token:
        return None

    try:
        payload = verify_token(token)
        user_id = payload.get('sub')
        if user_id is None:
            return None

        # 从数据库获取用户
        user = db.query(User).filter(User.id == int(user_id)).first()
        if user is None or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="用户不存在或已禁用"
            )
        return user
    except HTTPException:
        raise
    except Exception:
        # 令牌解析失败，返回None（允许匿名访问）
        return None

async def get_current_active_user(current_user: Optional[User] = Depends(get_current_user)):
    """获取当前活跃用户（必须登录）"""
    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='需要登录'
        )
    return current_user

async def get_current_active_superuser(current_user: User = Depends(get_current_active_user())):
    """获取当前超级用户"""
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='需要管理员权限',
        )
    return current_user

async def create_access_token():
    pass