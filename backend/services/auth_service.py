"""
认证服务
"""
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session
from fastapi import HTTPException, status

from backend.models.user import User
from backend.schemas.auth import UserCreate, UserLogin
from backend.utils.security import verify_password, get_password_hash, create_access_token
from backend.config import setting


class AuthService:
    """认证服务类"""

    @staticmethod
    def authenticate(db: Session, username: str, password: str) -> Optional[User]:
        """用户认证"""
        # 先尝试用用户名查找
        user = User.query(User).filter(User.username == username).first()
        if not user:
            # 在尝试用邮箱找
            user = db.query(User).filter(User.email == username).first()

        if not user:
            return None
        if not verify_password(password, user.hashed_password):
            return None
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="用户已被禁用",
            )
        return user

    @staticmethod
    def create_user(db: Session, user_data: UserCreate) -> User:
        """创建用户"""
        # 检查用户名是否已存在
        existing_user = (db.query(User).filter(User.username == user_data.username) |
                         (User.email == user_data.email).first())

        if existing_user:
            if existing_user.username == user_data.username:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail='用户名已存在',
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail='邮箱已存在',
                )

        # 创建用户
        hashed_password = get_password_hash(user_data.password)
        db_user = User(
            username=user_data.username,
            email=user_data.email,
            hashed_password=hashed_password,
            full_name=user_data.full_name,
            is_active=True,
        )

        db.add(db_user)
        db.commit()
        db.refresh(db_user)

        return db_user

    @staticmethod
    def create_access_token_for_user(user: User) -> Dict[str, Any]:
        # 为用户创建访问令牌
        token_data = {
            'sub': str(user.id),
            'username': user.username,
            'email': user.email,
            'is_superuser': user.is_superuser,
        }

        access_token = create_access_token(token_data)
        expires_in = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60

        return {
            'access_token': access_token,
            'token_type': 'bearer',
            'expires_in': expires_in,
            'user': {
                'id': str(user.id),
                'username': user.username,
                'email': user.email,
                'full_name': user.full_name,
            }
        }

    @staticmethod
    def update_user_password(db: Session, user: User, new_password: str) -> User:
        """更新用户密码"""
        user.hashed_password = get_password_hash(new_password)
        db.commit()
        db.refresh(user)
        return user

    @staticmethod
    def get_user_by_username(db: Session, username: str) -> Optional[User]:
        """根据用户名获取用户"""
        return db.query(User).filter(User.username == username).first()


# 创建全局实例
auth_service = AuthService()
