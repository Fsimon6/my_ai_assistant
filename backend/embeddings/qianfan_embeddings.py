"""
Qianfan嵌入模型
"""
import os
from typing import List, Optional, Dict, Any
# import qianfan
import openai
from qianfan import Qianfan
from .base_embedding import BaseEmbedding, EmbeddingResult


class QianfanEmbeddings(BaseEmbedding):
    """Qianfan嵌入模型"""

    def __init__(self, model_name: str = 'ERNIE-3.5-8k', api_key: Optional[str] = None, **kwargs):
        super().__init__(model_name, api_key, **kwargs)

        # 设置模型对应的维度
        self._set_model_dimensions(model_name)

        # 初始化QianFan客户端
        self.api_key = api_key or os.getenv('QIANFAN_API_KEY')
        if not self.api_key:
            raise ValueError('QIANFAN_API_KEY is required')
        self.client = Qianfan(api_key=self.api_key)
        self._initialized = True

    def _set_model_dimensions(self, model_name: str):
        """根据模型名称设置维度"""
        dimension_map = {
            'ERNIE-3.5-8k': 1536,
        }
        self.dimensions = dimension_map.get(model_name, 1536)

    def initialize(self):
        """初始化（已在前初始化）"""
        if not self._initialized:
            self.client = Qianfan(api_key=self.api_key)
            self._initialized = True

    def embed(self, texts: List[str]) -> EmbeddingResult:
        """批量生成嵌入向量"""
        if not texts:
            return EmbeddingResult([], self.model_name, self.dimensions, 0)

        try:
            # 分批处理以避免令牌限制
            all_embeddings = []
            total_tokens = 0

            for i in range(0, len(texts), self.batch_size):
                batch = texts[i:i + self.batch_size]
                response = self.client.embeddings.create(
                    model=self.model_name,
                    input=batch,
                    encoding_format='float'
                )

                batch_embeddings = [data.embedding for data in response.data]
                all_embeddings.extend(batch_embeddings)

                # 估算令牌数
                total_tokens += sum(len(text.split()) for text in batch)

            return EmbeddingResult(
                embeddings=all_embeddings,
                model=self.model_name,
                dimensions=self.dimensions,
                total_tokens=total_tokens,
                metadata={
                    'provider': 'qianfan',
                    'model': self.model_name,
                    'batch_count': (len(texts) + self.batch_size - 1) // self.batch_size,
                }
            )

        except openai.APIError as e:
            raise Exception(f'嵌入生成失败：{str(e)}')

    def embed_single(self, text: str) -> List[float]:
        """生成单个文本的嵌入向量"""
        result = self.embed([text])
        return result.embeddings[0] if result.embeddings else []

    def get_usage_info(self) -> Dict[str, Any]:
        """获取使用信息"""
        # 注意：实际使用量需要通过QianFan dashboard获取
        return {
            'provider': 'qianfan',
            'model': self.model_name,
            'dimensions': self.dimensions,
            'estimated_cost_per_1k_tokens': 0.0001  # 示例价格
        }
