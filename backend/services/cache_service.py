import logging
from typing import Any
from datetime import timedelta

logger = logging.getLogger(__name__)


class CacheService:
    """缓存服务"""

    def __init__(self):
        self._cache = {}     # 内存缓存（可替换为Redis）
        self.enabled = True

    async def get(self, key: str, default: Any = None) -> Any:
        """获取缓存"""
        if not self.enabled:
            return default

        try:
            cached = self._cache.get(key)
            if cached and not self._is_expired(cached):
                logger.debug(f'缓存命中：{key}')
                return cached['data']
            return default
        except Exception as e:
            logger.error(f'获取缓存失败：{e}')
            return default

    async def set(self, key: str, value: Any, ttl: int = 300) -> bool:
        """设置缓存"""
        if not self.enabled:
            return False

        try:
            self._cache[key] = {
                'data': value,
                'expires_at': self._get_expiration_time(ttl)
            }
            logger.debug(f'缓存设置：{key}，TTL:{ttl}s')
            return True
        except Exception as e:
            logger.error(f'设置缓存失败：{e}')
            return False

    async def delete(self, key: str) -> bool:
        """删除缓存"""
        try:
            if key in self._cache:
                del self._cache[key]
                logger.debug(f'缓存删除：{key}')
            return True
        except Exception as e:
            logger.error(f'删除缓存失败：{e}')
            return False

    async def clear(self) -> bool:
        """清空缓存"""
        try:
            self._cache.clear()
            logger.info('缓存已清空')
            return True
        except Exception as e:
            logger.error(f'清空缓存失败：{e}')
            return False

    def _get_expiration_time(self, ttl: int) -> float:
        """获取过期时间"""
        from datetime import datetime
        return (datetime.now() + timedelta(seconds=ttl)).timestamp()

    def _is_expired(self, cached_item: dict) -> bool:
        """检查是否过期"""
        from datetime import datetime
        return datetime.now().timestamp() > cached_item.get('expires_at', 0)


# 缓存键生成器
class CacheKey:
    """缓存键生成工具类"""

    @staticmethod
    def rag_query(query: str, context_count: int = 3) -> str:
        """RAG查询缓存键"""
        import hashlib
        key_str = f'rag_query:{query}:{context_count}'
        return f'cache:{hashlib.md5(key_str.encode()).hexdigest()}'

    @staticmethod
    def document_chunks(document_id: str) -> str:
        """文档chunks缓存键"""
        return f'document:chunks:{document_id}'

    @staticmethod
    def embedding(text: str, model: str = 'default') -> str:
        """Embedding缓存键"""
        import hashlib
        kay_str = f'embedding:{model}:{text}'
        return f'cache:{hashlib.md5(kay_str.encode()).hexdigest()}'


# 单例实例
_cache_service = None


def get_cache_service() -> CacheService:
    """获取缓存服务单例"""
    global _cache_service
    if _cache_service is None:
        _cache_service = CacheService()
    return _cache_service
