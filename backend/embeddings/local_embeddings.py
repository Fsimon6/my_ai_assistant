"""
本地嵌入模型实现（使用Sentence Transformers）
"""
from typing import List, Optional, Dict, Any
from sentence_transformers import SentenceTransformer
import torch
from .base_embedding import BaseEmbedding, EmbeddingResult


class LocalEmbeddings(BaseEmbedding):
    """本地嵌入模型（使用Sentence Transformers）"""

    def __init__(self, model_name: str = 'ERNIE-3.5-8k', device: Optional[str] = None, **kwargs):
        super().__init__(model_name, **kwargs)

        # 设置设备
        self.device = device or ('cuda' if torch.cuda.is_available() else 'cpu')

        # 初始化模型
        self.model = None
        self._initialized = False

    def initialize(self):
        """初始化模型"""
        if self._initialized:
            return

        try:
            print(f'正在加载本地嵌入模型：{self.model_name}')
            self.model = SentenceTransformer(self.model_name, device=self.device)

            # 测试模型及获取维度
            test_embedding = self.model.encode(['test'])
            self.dimensions = test_embedding.shape[1]
            self._initialized = True
            print(f'本地嵌入模型加载完成，维度：{self.dimensions}')

        except Exception as e:
            raise Exception(f'本地模型加载失败：{str(e)}')

    def embed(self, texts: List[str]) -> EmbeddingResult:
        """批量生成嵌入向量"""
        if not self._initialized:
            self.initialize()

        if not texts:
            return EmbeddingResult([], self.model_name, self.dimensions, 0)

        try:
            # 编码文本
            embeddings = self.model.encode(
                texts,
                batch_size=self.batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
                normalize_embeddings=True,
            )

            # 转换为列表格式
            embeddings_list = embeddings.tolist()

            # 估算令牌数（粗略估计）
            total_tokens = sum(len(text.split()) for text in texts)
            return EmbeddingResult(
                embeddings=embeddings_list,
                model=self.model_name,
                dimensions=self.dimensions,
                total_tokens=total_tokens,
                metadata={
                    'provider': 'local',
                    'model': self.model_name,
                    'device': self.device,
                    'normalized': True
                }
            )

        except Exception as e:
            raise Exception(f'本地嵌入生成失败：{str(e)}')

    def embed_single(self, text: str) -> List[float]:
        """生成单个文本的嵌入向量"""
        result = self.embed([text])
        return result.embeddings[0] if result.embeddings else []

    def get_model_info(self) -> Dict[str, Any]:
        """获取模型详细信息"""
        info = super().get_model_info()
        info.update({
            'device': self.device,
            'model_type': 'sentence-transformers',
            'normalized': True
        })

        if self.model:
            info.update({
                'max_seq_length': self.model.max_seq_length,
                'model_architecture': str(type(self.model)),
            })

        return info

    @classmethod
    def get_available_embeddings(cls) -> List[Dict[str, str]]:
        """获取可用的本地模型列表"""
        return [{
                'name': 'ERNIE-3.5-8k',
                'description': '小而快的嵌入模型',
                'dimensions': 512,
                'language': 'zh'
                }]
