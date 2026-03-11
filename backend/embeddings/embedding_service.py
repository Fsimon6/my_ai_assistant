"""
嵌入服务
统一管理多种嵌入模型
"""
import os
from typing import List, Optional, Dict, Any, Union
from enum import Enum
from .base_embedding import BaseEmbedding, EmbeddingResult
from .qianfan_embeddings import QianfanEmbeddings
from .local_embeddings import LocalEmbeddings
from .models import EmbeddingConfig, EmbeddingType
from .cache_manager import EmbeddingCache


class EmbeddingService:
    """嵌入服务"""

    def __init__(self, config: Optional[EmbeddingConfig] = None):
        self.config = config or EmbeddingConfig()
        self.cache = EmbeddingCache()
        self._embedding_model = None
        self._initialized = False

    def initialize(self):
        """初始化嵌入服务"""
        if self._initialized:
            return

        try:
            if self.config.embedding_type == EmbeddingType.QIANFAN:
                api_key = self.config.qianfan_api_key or os.getenv('QIANFAN_API_KEY')
                if not api_key:
                    raise ValueError('Qianfan API Key is required')
                self._embedding_model = QianfanEmbeddings(
                    model_name=self.config.model_name,
                    api_key=api_key,
                    dimensions=self.config.dimensions,
                )

            elif self.config.embedding_type == EmbeddingType.LOCAL:
                self._embedding_model = LocalEmbeddings(
                    model_name=self.config.model_name,
                    device=self.config.device,
                )

            else:
                raise ValueError(f'不支持的嵌入模型{self.config.model_name}')

        except Exception as e:
            raise Exception(f'嵌入服务初始化失败：{str(e)}')

    def embed(self, texts: List[str], use_cache: bool = True) -> EmbeddingResult:
        """生成嵌入向量"""
        if not self._initialized:
            self.initialize()

        if not texts:
            return EmbeddingResult([], self.config.model_name, self.config.dimensions, 0)

        # 检查缓存
        cached_results = []
        texts_to_process = []

        if use_cache:
            for text in texts:
                cached = self.cache.get(text, self.config.model_name)
                if cached:
                    cached_results.append(cached)
                else:
                    texts_to_process.append(text)
        else:
            texts_to_process = texts

        # 处理未缓存的文本
        new_results = None
        if texts_to_process:
            new_results = self._embedding_model.embed(texts_to_process)

            # 缓存结果
            if use_cache:
                for text, embedding in zip(texts_to_process, new_results.embeddings):
                    self.cache.set(text, self.config.model_name)

        # 合并结果
        if not cached_results:
            return new_results or EmbeddingResult([], self.config.model_name, self.config.dimensions, 0)

        if not new_results:
            return self._merge_cached_results(cached_results)

        # 合并缓存和新的结果
        all_embeddings = []
        text_index = 0

        for text in texts:
            cached = self.cache.get(text, self.config.model_name)
            if cached:
                all_embeddings.append(cached)
            else:
                all_embeddings.append(new_results.embeddings[text_index])
                text_index += 1

        return EmbeddingResult(
            embeddings=all_embeddings,
            model=self.config.model_name,
            dimensions=self.config.dimensions,
            total_tokens=new_results.total_tokens,  # 只计算新的
            metadata={
                'cached_count': len(cached_results),
                'new_count': len(texts_to_process),
                'provider': self.config.embedding_type.value
            }
        )

    def embed_single(self, text: str, use_cache: bool = True) -> List[float]:
        """生成单个文本的嵌入向量"""
        result = self.embed([text], use_cache)
        return result.embeddings[0] if result.embeddings else []

    def _merge_cached_results(self, cached_embeddings: List[List[float]]) -> EmbeddingResult:
        """合并缓存的嵌入结果"""
        return EmbeddingResult(
            embeddings=cached_embeddings,
            model=self.config.model_name,
            dimensions=self.config.dimensions,
            total_tokens=0,     # 缓存的不计令牌
            metadata={
                'cached_only': True,
                'count': len(cached_embeddings),
            }
        )

    def cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """计算余弦相似度"""
        return self._embedding_model.cosine_similarity(vec1, vec2)

    def batch_cosine_similarity(self, query_vec: List[float], doc_vecs: List[List[float]]) -> List[float]:
        """批量计算余弦相似度"""
        return self._embedding_model.batch_cosine_similarity(query_vec, doc_vecs)

    def search_similar(self, query: str, documents: List[str], top_k: int = 5) -> List[Dict[str, Any]]:
        """搜索相似文档"""
        # 生成查询嵌入
        query_embedding = self.embed_single(query)

        # 生成文档嵌入
        doc_embeddings = self.embed(documents)

        # 计算相似度
        similarities = self.batch_cosine_similarity(query_embedding, doc_embeddings.embeddings)

        # 排序并返回结果
        result = []
        for i, (doc, sim) in enumerate(zip(documents, similarities)):
            result.append({
                'index': i,
                'document': doc[:200] + '...' if len(doc) > 200 else doc,
                'similarity': float(sim),
                'embedding': doc_embeddings.embeddings[i] if i < len(doc_embeddings.embeddings) else [],
            })

        # 按相似度降序排序
        result.sort(key=lambda x: x['similarity'], reverse=True)
        return result[:top_k]

    def get_service_info(self) -> Dict[str, Any]:
        """获取服务信息"""
        return {
            'initialized': self._initialized,
            'config': self.config.dict() if self.config else None,
            'model_info': self._embedding_model.get_model_info() if self._embedding_model else None,
            'cache_stats': self.cache.get_stats()
        }