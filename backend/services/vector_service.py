# -*- coding: utf-8 -*-
import asyncio
import os
from typing import List, Dict, Any, Optional
import logging
import uuid
from services.types import SearchResult

from langchain_classic.vectorstores import Chroma
from langchain_classic.embeddings.base import Embeddings

from services.llm_service import get_llm

logger = logging.getLogger(__name__)


class AIAssistantEmbeddings(Embeddings):
    """AI助手自定义Embeddings类"""

    def __init__(self):
        self.llm = get_llm()

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """嵌入文档列表"""
        try:
            embeddings = asyncio.run(self.llm.generate_embeddings(texts))
            return embeddings
        except Exception as e:
            logger.error(f'嵌入文档失败：{e}')

            # 回退到本地模型
            try:
                from langchain_community.embeddings import HuggingFaceEmbeddings
                local_embeddings = HuggingFaceEmbeddings(
                    model_name='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',
                )
                return local_embeddings.embed_documents(texts)
            except Exception as local_e:
                logger.error(f'退回到本地模型失败：{local_e}')

    def embed_query(self, text: str) -> list[float]:
        """嵌入查询"""
        try:
            embeddings = asyncio.run(self.llm.generate_embeddings([text]))
            return embeddings[0] if embeddings else []
        except Exception as e:
            logger.error(f'嵌入查询失败：{e}')

            # 回退到本地模型
            try:
                from langchain_community.embeddings import HuggingFaceEmbeddings
                local_embeddings = HuggingFaceEmbeddings(
                    model_name='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2',
                )
                return local_embeddings.embed_query(text)
            except Exception as local_e:
                logger.error(f'加载本地嵌入模型失败：{local_e}')


class VectorStoreManager:
    """向量存储管理器"""

    def __init__(self, persist_directory: str = './data/chroma_db'):
        self.persist_directory = persist_directory
        os.makedirs(persist_directory, exist_ok=True)

        # 初始化embeddings
        self.embeddings = AIAssistantEmbeddings()

        # 初始化Chroma
        self.vector_store = Chroma(
            persist_directory=persist_directory,
            embedding_function=self.embeddings,
            collection_name='ai_assistant_docs',
        )

    async def add_documents(
        self,
        documents: List[Dict[str, Any]],
        collection_name: str = 'ai_assistant_docs',
    ) -> List[str]:
        """添加文档到向量数据库"""
        try:
            # 提取内容和元数据
            contents = [doc['content'] for doc in documents]
            metadatas = [doc['metadata'] for doc in documents]
            ids = [doc['id'] for doc in documents]

            # 添加到向量库
            self.vector_store = Chroma.from_texts(
                texts=contents,
                metadatas=metadatas,
                embedding=self.embeddings,
                ids=ids,
                persist_directory=self.persist_directory,
                collection_name=collection_name,
            )

            # 持久化
            self.vector_store.persist()

            logger.info(f'成功添加{len(documents)}个文档到向量数据库')
            return ids

        except Exception as e:
            logger.error(f'添加文档到向量数据库失败：{e}')
            raise

    async def search(
        self,
        query_embedding,
        query: str,
        k: int = 5,
        filter_dict: Optional[Dict] = None,
    ) -> List[SearchResult]:
        """相似度搜索"""
        try:
            # 执行搜索
            results = self.vector_store.similarity_search_with_relevance_scores(
                query=query,
                k=k,
                filter=filter_dict
            )

            # 格式化结果
            formatted_results = []
            for doc, score in results:
                formatted_results.append({
                    'content': doc.page_content,
                    'metadata': doc.metadata,
                    'score': score,
                    'id': doc.metadata.get('id', str(uuid.uuid4())),
                })

            logger.info(f'搜索查询"{query}"返回{len(formatted_results)}个结果')
            return formatted_results

        except Exception as e:
            logger.error(f'向量搜索失败：{e}')
            raise

    async def delete_documents(
        self,
        ids: List[str],
        # collection_name: str = 'ai_assistant_docs',
    ) -> bool:
        """删除文档"""
        try:
            # Chroma的delete方法
            collection = self.vector_store._collection
            if collection:
                collection.delete(ids=ids)
                self.vector_store.persist()
            logger.info(f'成功删除{len(ids)}个文档')
            return True

        except Exception as e:
            logger.error(f'删除文档失败：{e}')
            return False

    def get_collection_info(self) -> Dict[str, Any]:
        """获取集合信息"""
        try:
            collection = self.vector_store._collection
            if collection:
                count = collection.count()
                return {
                    'total_documents': count,
                    'collection_name': 'ai_assistant_docs',
                    'persist_directory': self.persist_directory,
                }
            return {'total_documents': 0}
        except Exception as e:
            logger.error(f'获取集合信息失败：{e}')
            return {'total_documents': 0}


# 单例实例
_vector_store_manager = None


def get_vector_store_manager() -> VectorStoreManager:
    """获取向量存储管理器单例"""
    global _vector_store_manager
    if _vector_store_manager is None:
        _vector_store_manager = VectorStoreManager()
    return _vector_store_manager
