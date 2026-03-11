from typing import List, Dict, Any, Optional
from backend.rag.retriever import Retriever
from backend.rag.chain import RAGChain
from backend.services.vector_service import VectorStoreManager
from backend.services.llm_service import BaseLLM, get_llm


class RAGPipeline:
    """完整的 RAG 流水线：检索 + 生成"""

    def __init__(
        self,
        vector_manager: VectorStoreManager,
        llm: Optional[BaseLLM] = None,
        top_k: int = 5,
    ):
        self.vector_manager = vector_manager
        self.llm = llm or get_llm()     # 如果没有传入 llm，使用默认实例
        self.retriever = Retriever(vector_manager, top_k)
        self.chain = RAGChain(self.llm)

    async def query(
        self,
        question: str,
        filter_dict: Optional[Dict] = None,
        history: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        """处理一个问题，返回答案和来源"""
        # 1. 检索
        docs = await self.retriever.retrieve(question, filter_dict)

        # 2. 生成
        answer = await self.chain.run(question, docs, history)
        return {
            "answer": answer,
            "sources": docs,
        }
