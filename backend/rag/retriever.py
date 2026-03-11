from typing import List, Dict, Any, Optional
from backend.services.vector_service import VectorStoreManager


class Retriever:
    """检索器：根据查询从向量数据库检索相关文档块"""

    def __init__(self, vector_manager: VectorStoreManager, top_k: int = 5):
        self.vector_manager = vector_manager
        self.top_k = top_k

    async def retrieve(self, query: str, filter_dict: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """检索相关文档"""
        # 直接调用 vector_service 的 search 方法（它内部会处理 embedding）
        results = await self.vector_manager.search(
            query=query,
            k=self.top_k,
            filter_dict=filter_dict
        )
        return results
