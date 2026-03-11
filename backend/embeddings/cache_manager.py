"""
嵌入缓存管理器
"""
import hashlib
import json
import time
from typing import List, Optional, Dict, Any
from collections import OrderedDict
import threading


class EmbeddingCache:
    """嵌入缓存"""

    def __init__(self, max_size: int = 1024, ttl: int = 3600):
        self.max_size = max_size
        self.ttl = ttl  # 生存时间（秒）
        self.cache = OrderedDict()
        self.lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def _get_key(self, text: str, model_name: str) -> str:
        """生成缓存键"""
        key_str = f'{model_name}: {text}'
        return hashlib.md5(key_str.encode('utf-8')).hexdigest()

    def get(self, text: str, model_name: str) -> Optional[List[float]]:
        """获取缓存值"""
        key = self._get_key(text, model_name)
        with self.lock:
            if key in self.cache:
                entry = self.cache[key]

                # 检查是否过期
                if time.time() - entry['timestamp'] < self.ttl:
                    # 移动到最新(LRU)
                    self.cache.move_to_end(key)
                    self.hits += 1
                    return entry['embedding']
                else:
                    # 过期，删除
                    del self.cache[key]

            self.misses += 1
            return None

    def set(self, text: str, embedding: List[float], model_name: str):
        """设置缓存值"""
        key = self._get_key(text, model_name)

        with self.lock:
            # 如果缓存已满，删除最旧的
            if len(self.cache) >= self.max_size:
                self.cache.popitem(last=False)
            self.cache[key] = {
                'embedding': embedding,
                'timestamp': time.time(),
                'text_hash': hashlib.md5(text.encode('utf-8')).hexdigest(),
                'model': model_name,
            }

    def delete(self, text: str, model_name: str) -> bool:
        """删除缓存值"""
        key = self._get_key(text, model_name)

        with self.lock:
            if key in self.cache:
                del self.cache[key]
                return True
            return False

    def clear(self):
        """清空缓存"""
        with self.lock:
            self.cache.clear()
            self.hits = 0
            self.misses = 0

    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        with self.lock:
            total = self.hits + self.misses
            hit_rate = self.hits / total if total > 0 else 0
            return {
                'size': len(self.cache),
                'max_size': self.max_size,
                'hits': self.hits,
                'misses': self.misses,
                'hit_rate': hit_rate,
                'ttl': self.ttl,
            }

    def export(self) -> Dict[str, Any]:
        """导出缓存（用于持久化）"""
        with self.lock:
            return {
                'max_size': self.max_size,
                'ttl': self.ttl,
                'entries': list(self.cache.items()),
                'stats': self.get_stats(),
            }

    def import_data(self, data: Dict[str, Any]):
        """导入缓存数据"""
        with self.lock:
            self.max_size = data.get('max_size', self.max_size)
            self.ttl = data.get('ttl', self.ttl)
            entries = data.get('entries', [])
            self.cache.clear()

            for key, value in entries:
                self.cache[key] = value

            stats = data.get('stats', {})
            self.hits = stats.get('hits', 0)
            self.misses = stats.get('misses', 0)

    def save_to_file(self, filepath: str):
        """保存缓存到文件"""
        data = self.export()

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_from_file(self, filepath: str):
        """从文件加载缓存"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self.import_data(data)
            return True
        except Exception:
            return False

    def cleanup_expired(self):
        """清理过期条目"""
        with self.lock:
            current_time = time.time()
            expired_keys = []

            for key, entry in self.cache.items():
                if current_time - entry['timestamp'] >= self.ttl:
                    expired_keys.append(key)

            for key in expired_keys:
                del self.cache[key]

            return len(expired_keys)