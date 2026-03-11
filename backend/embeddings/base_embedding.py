"""
基础嵌入模型抽象类
"""
from abc import ABC, abstractmethod
from typing import List, Optional, Union, Dict, Any
from dataclasses import dataclass
from pydantic import BaseModel
import numpy as np


@dataclass
class EmbeddingResult:
    """嵌入结果"""
    embeddings: List[List[float]]
    model: str
    dimensions: int
    total_tokens: int
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}

    def to_numpy(self) -> np.ndarray:
        """转换为numpy数组"""
        return np.array(self.embeddings)

    def get_average_embedding(self) -> List[float]:
        """获取平均嵌入向量"""
        if not self.embeddings:
            return []
        return np.mean(self.embeddings, axis=0).tolist()


class BaseEmbedding(ABC):
    """基础嵌入模型抽象类"""

    def __init__(self, model_name: str, api_key: Optional[str] = None, **kwargs):
        self.model_name = model_name
        self.api_key = api_key
        self.dimensions = kwargs.get("dimensions", 1536)
        self.batch_size = kwargs.get("batch_size", 32)
        self.max_length = kwargs.get("max_length", 8192)
        self._initialized = False

    @abstractmethod
    def initialize(self):
        """初始化模型"""
        pass

    @abstractmethod
    def embed(self, text: List[str]) -> EmbeddingResult:
        """生成嵌入向量"""
        pass

    @abstractmethod
    def embed_single(self, text: str) -> List[float]:
        """生成单个文本的嵌入向量"""
        pass

    @abstractmethod
    def cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """计算余弦相似度"""
        if not vec1 or not vec2:
            return 0.0

        vec1_np = np.array(vec1)
        vec2_np = np.array(vec2)

        # 归一化
        norm1 = np.linalg.norm(vec1_np)
        norm2 = np.linalg.norm(vec2_np)

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return float(np.dot(vec1_np, vec2_np) / (norm1 * norm2))

    def batch_cosine_similarity(self, query_vec: List[float], doc_vecs: List[List[float]]) -> List[float]:
        """批量计算余弦相似度"""
        if not query_vec or not doc_vecs:
            return []

        query_np = np.array(query_vec)
        doc_np = np.array(doc_vecs)

        # 归一化
        query_norm = np.linalg.norm(query_np)
        doc_norms = np.linalg.norm(doc_np)

        # 避免除以零
        valid_indices = doc_norms > 0
        similarities = np.zeros(len(query_vec))

        if query_norm > 0:
            similarities[valid_indices] = np.dot(doc_np[valid_indices], query_np) / (doc_norms[valid_indices] * query_norm)
        return similarities.tolist()

    def get_model_info(self) -> Dict[str, Any]:
        """获取模型信息"""
        return {
            'model_name': self.model_name,
            'dimensions': self.dimensions,
            'batch_size': self.batch_size,
            'max_length': self.max_length,
            'initialized': self._initialized,
        }