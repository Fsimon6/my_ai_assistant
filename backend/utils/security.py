# -*- coding: utf-8 -*-
"""
安全相关工具函数
"""
import os
import base64
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from jose import jwt, JWTError
from fastapi import HTTPException, status
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import logging

from backend.config.settings import settings


logger = logging.getLogger(__name__)


# JWT配置
SECRET_KEY = settings.JWT_SECRET
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = settings.ACCESS_TOKEN_EXPIRE_MINUTES


def _bcrypt_truncated(password: str) -> bytes:
    """bcrypt 对明文有 72 字节上限，超出直接抛异常。

    统一在 utf-8 编码后截断到 72 字节：既符合 bcrypt 规范，
    也避免截断多字节字符（按字节切而不是按字符切）。
    get_password_hash 与 verify_password 必须使用同一截断规则，
    否则同样的明文在“存”和“验”时会被截断成不同字节，导致校验永远失败。
    """
    return password.encode("utf-8")[:72]


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码（直接使用 bcrypt；与 get_password_hash 同样生成 $2b$ 哈希，兼容历史哈希）"""
    import bcrypt
    try:
        return bcrypt.checkpw(
            _bcrypt_truncated(plain_password),
            hashed_password.encode("utf-8"),
        )
    except (ValueError, TypeError):
        # 存储的哈希格式损坏/非 $2b$ 等异常：按校验失败处理，不冒泡成 500
        return False


def get_password_hash(password: str) -> str:
    """生成密码哈希（bcrypt $2b$，明文截断到 72 字节）"""
    import bcrypt
    return bcrypt.hashpw(_bcrypt_truncated(password), bcrypt.gensalt()).decode("utf-8")


def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    """创建访问令牌"""
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str) -> dict:
    """解码访问令牌，返回 payload"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.JWTError as e:
        raise ValueError("Invalid token") from e


def verify_token(token: str) -> Dict[str, Any]:
    """验证JWT令牌"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_current_user_from_token(token: str):
    """从令牌获取当前用户"""
    payload = verify_token(token)
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='无效的认证令牌'
        )
    return {'user_id': user_id, 'username': payload.get('username')}


class SecretManager:
    """密钥管理器（新增）"""

    def __init__(self, master_key: str = None):
        self.master_key = master_key or settings.JWT_SECRET
        self.fernet = self._create_fernet()

    def _create_fernet(self):
        """创建Fernet加密器"""
        salt = b'ai_assistant_salt'     # 生产环境应该使用随机salt存储
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(self.master_key.encode()))
        return Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        """加密文本"""
        try:
            encrypted = self.fernet.encrypt(plaintext.encode())
            return encrypted.decode()
        except Exception as e:
            logger.error(f'加密失败：{e}')
            raise

    def decrypt(self, ciphertext: str) -> str:
        """解密文本"""
        try:
            decrypted = self.fernet.decrypt(ciphertext.encode())
            return decrypted.decode()
        except Exception as e:
            logger.error(f'解密失败：{e}')
            raise

    def hash_api_key(self, api_key: str) -> str:
        """哈希API密钥（用于存储验证）"""
        salt = os.getenv('API_KEY_SALT', 'ai_assistant_salt')
        return hashlib.sha256(f'{api_key}{salt}'.encode()).hexdigest()

    def validate_api_key(self, api_key: str, hashed_key: str) -> bool:
        """验证API密钥"""
        return self.hash_api_key(api_key) == hashed_key


class APIKeyManager:
    """API密钥管理器（新增）"""

    def __init__(self):
        self.secret_manager = SecretManager()
        self.cache = {}

    def get_provider_key(self, provider: str) -> str:
        """获取大模型提供商API密钥（统一使用通用 API_KEY）"""
        # Ollama / local 不要求 API Key
        if provider in ('ollama', 'local'):
            return ''

        api_key = settings.API_KEY
        if not api_key:
            raise ValueError('未设置环境变量：API_KEY')

        cache_key = f'{provider}_api_key'
        if cache_key not in self.cache:
            self.cache[cache_key] = api_key

        return self.cache[cache_key]

    def mask_key(self, key: str) -> str:
        """掩码显示密钥（用于日志）"""
        if not key or len(key) <= 8:
            return '***'
        return f'{key[:4]}...{key[-4:]}'

    def get_all_provider_keys(self) -> Dict[str, str]:
        """获取所有支持的供应商密钥（掩码后）"""
        providers = ['openai', 'dashscope', 'zhipu', 'ollama', 'local']
        result = {}

        for provider in providers:
            if provider in ('ollama', 'local'):
                result[provider] = '不需要'
                continue
            key = settings.API_KEY
            if key:
                result[provider] = self.mask_key(key)
            else:
                result[provider] = '未设置'

        return result


# ========单例实例和导出========
_api_key_manager = None


def get_api_key_manager() -> APIKeyManager:
    """获取API密钥管理器单例"""
    global _api_key_manager
    if _api_key_manager is None:
        _api_key_manager = APIKeyManager()
    return _api_key_manager


def get_secret_manager() -> SecretManager:
    """获取密钥管理器实例"""
    return SecretManager()


# ========导出列表========
__all__ = [
    # 原有JWT认证函数
    'verify_password',
    'get_password_hash',
    'create_access_token',
    'verify_token',
    'get_current_user_from_token',

    # 新增类和函数
    'SecretManager',
    'APIKeyManager',
    'get_api_key_manager',
    'get_secret_manager',
]
