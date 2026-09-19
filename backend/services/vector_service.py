# -*- coding: utf-8 -*-
import asyncio
import os
import concurrent.futures
from typing import List, Dict, Any, Optional
import logging
import uuid
from backend.services.types import SearchResult

from langchain_classic.vectorstores import Chroma
from langchain_classic.embeddings.base import Embeddings

from backend.services.llm_service import get_llm
from backend.config.settings import settings

logger = logging.getLogger(__name__)


class EmbeddingError(Exception):
    """Embedding 生成失败：禁止返回空向量或静默回退。"""
    pass


def _run_async(coro):
    """在独立线程中运行协程，避免被 LangChain 在“运行中事件循环”内同步调用时
    asyncio.run() 报 “cannot be called from a running event loop”。

    embed_documents / embed_query 是同步回调（LangChain Chroma 在添加/检索时同步调用），
    而 RAG 链路本身运行在 async 事件循环里，因此必须用独立线程 + 新事件循环执行。
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


class AIAssistantEmbeddings(Embeddings):
    """远程/云端 embedding（通过 LLM provider 的 embedding API）。

    - embedding model 来自 settings.EMBEDDING_MODEL，与 LLM_MODEL（Chat）解耦；
    - 远程 embedding 调用失败时显式 raise，绝不返回空向量、也绝不静默回退到未安装的本地模型，
      避免把无有效向量的文档写入 Chroma。
    """

    def __init__(self):
        self.llm = get_llm()
        self.embedding_model = settings.EMBEDDING_MODEL

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """嵌入文档列表；失败抛出 EmbeddingError，禁止空向量写入。"""
        try:
            vectors = _run_async(
                self.llm.generate_embeddings(texts, model=self.embedding_model)
            )
        except Exception as e:
            raise EmbeddingError(f'远程 embedding 调用失败：{e}') from e
        self._validate_vectors(vectors, expected=len(texts))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        """嵌入查询；失败抛出 EmbeddingError。"""
        try:
            vectors = _run_async(
                self.llm.generate_embeddings([text], model=self.embedding_model)
            )
        except Exception as e:
            raise EmbeddingError(f'远程 embedding 调用失败：{e}') from e
        self._validate_vectors(vectors, expected=1)
        return vectors[0]

    @staticmethod
    def _validate_vectors(vectors, expected: int) -> None:
        if not vectors or len(vectors) != expected:
            raise EmbeddingError(
                f'embedding 返回数量异常：期望 {expected} 条，实际 '
                f'{len(vectors) if vectors else 0} 条'
            )
        for v in vectors:
            if not isinstance(v, (list, tuple)) or len(v) == 0:
                raise EmbeddingError('embedding 返回空向量，已禁止写入 Chroma')


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
        """添加文档到向量数据库（复用单例 Chroma 客户端，避免每次上传重建客户端导致
        查询/删除状态不一致）"""
        try:
            # 提取内容和元数据
            contents = [doc['content'] for doc in documents]
            metadatas = [doc['metadata'] for doc in documents]
            ids = [doc['id'] for doc in documents]

            # 复用已有的单例客户端，向其追加文本（保持 add/query/delete 使用同一客户端）
            result_ids = self.vector_store.add_texts(
                texts=contents,
                metadatas=metadatas,
                ids=ids,
            )

            logger.info(f'成功添加{len(documents)}个文档到向量数据库')
            return result_ids

        except Exception as e:
            logger.error(f'添加文档到向量数据库失败：{e}')
            raise

    async def search(
        self,
        query: str,
        query_embedding=None,
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
        document_ids: List[str],
        user_id: Optional[int] = None,
    ) -> int:
        """按 document_id（文档级）+ user_id 精确删除向量。

        仅删除归属当前用户、且 document_id 在给定列表中的向量；不会误删其他文档或
        其他用户数据。返回实际删除的向量数量（0 表示无匹配 / 越权）。
        """
        try:
            collection = self.vector_store._collection
            if not collection or not document_ids:
                return 0
            target = set(document_ids)
            # 取出全量 id 与元数据，按 document_id + user_id 精确过滤
            existing = collection.get(include=['metadatas'])
            all_ids = existing.get('ids', [])
            all_metas = existing.get('metadatas', [])
            allowed_ids = [
                vid for vid, meta in zip(all_ids, all_metas)
                if meta and meta.get('document_id') in target
                and (user_id is None or meta.get('user_id') == user_id)
            ]
            if allowed_ids:
                collection.delete(ids=allowed_ids)
            logger.info(f'成功删除{len(allowed_ids)}/{len(document_ids)}个文档的向量')
            return len(allowed_ids)

        except Exception as e:
            logger.error(f'删除文档失败：{e}')
            return 0

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


    def list_user_documents(self, user_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """按归属用户列出其全部文档（聚合 document_id），仅返回该用户自己的文档，
        用于知识库文档列表。无需新增数据库表，直接基于 Chroma 向量 metadata 聚合。

        user_id: 服务端强制隔离；只聚合 metadata.user_id == user_id 的向量，杜绝跨用户泄露。
        """
        try:
            collection = self.vector_store._collection
            if not collection:
                return []
            data = collection.get(include=['metadatas'])
            all_metas = data.get('metadatas', []) or []

            docs: Dict[str, Dict[str, Any]] = {}
            for meta in all_metas:
                if not meta:
                    continue
                # 用户隔离：跳过非当前用户的向量
                if user_id is not None and meta.get('user_id') != user_id:
                    continue
                doc_id = meta.get('document_id')
                if not doc_id:
                    continue

                filename = meta.get('filename') or meta.get('source') or '未知文件'
                existed = docs.get(doc_id)
                if existed is None:
                    existed = docs[doc_id] = {
                        'document_id': doc_id,
                        'filename': filename,
                        'type': filename.rsplit('.', 1)[-1].lower() if '.' in filename else '',
                        'size': int(meta.get('file_size') or 0),
                        'chunks': 0,
                        'created_at': meta.get('processed_at'),
                        # doc_kind: 'document'（普通文本）| 'excel'（Phase 1A 表格）
                        'kind': meta.get('doc_kind', 'document'),
                    }
                    if meta.get('doc_kind') == 'excel':
                        existed['sheet_count'] = int(meta.get('sheet_count') or 0)
                existed['chunks'] += 1
                # 取最早的处理时间作为文档上传时间
                pa = meta.get('processed_at')
                if pa and (existed['created_at'] is None or pa < existed['created_at']):
                    existed['created_at'] = pa

            return list(docs.values())
        except Exception as e:
            logger.error(f'列出用户文档失败：{e}')
            return []

    async def get_document_chunks(
        self,
        document_id: str,
        user_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """按 document_id 读取某文档全部分块内容（服务端强制 user_id 隔离），供文档预览/查看原文。

        无需新增数据库表，直接基于 Chroma 向量（每个 chunk 已存 document_id + user_id + chunk_index）。
        """
        try:
            collection = self.vector_store._collection
            if not collection:
                return []
            # Chroma where 仅支持单操作符；多条件必须用 $and 包裹
            conds: List[Dict[str, Any]] = [{'document_id': document_id}]
            if user_id is not None:
                conds.append({'user_id': user_id})
            where: Dict[str, Any] = {'$and': conds} if len(conds) > 1 else conds[0]
            data = collection.get(where=where, include=['documents', 'metadatas'])
            docs = data.get('documents', []) or []
            metas = data.get('metadatas', []) or []

            chunks: List[Dict[str, Any]] = []
            for content, meta in zip(docs, metas):
                meta = meta or {}
                idx = int(meta.get('chunk_index', 0) or 0)
                chunks.append({
                    'index': idx,
                    'content': content,
                    'source': meta.get('source') or meta.get('filename') or '',
                })
            chunks.sort(key=lambda c: c['index'])
            return chunks
        except Exception as e:
            logger.error(f'读取文档分块失败：{e}')
            return []


# 单例实例
_vector_store_manager = None


def get_vector_store_manager() -> VectorStoreManager:
    """获取向量存储管理器单例"""
    global _vector_store_manager
    if _vector_store_manager is None:
        _vector_store_manager = VectorStoreManager()
    return _vector_store_manager
