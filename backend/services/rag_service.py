# -*- coding: utf-8 -*-
import os
from typing import List, Dict, Any, Optional
from collections.abc import AsyncIterator
import logging
from datetime import datetime

from services.llm_service import get_llm
from services.vector_service import get_vector_store_manager
from services.document_service import DocumentProcessor
from .cache_service import get_cache_service, CacheKey

logger = logging.getLogger(__name__)


class RagService:
    """RAG服务类(带缓存优化)"""

    def __init__(self):
        self.llm = get_llm()
        self.vector_store = get_vector_store_manager()
        self.document_processor = DocumentProcessor(
            chunk_size=800,
            chunk_overlap=150
        )
        self.cache = get_cache_service()
        logger.info(' RAG服务初始化（带缓存）')

    async def process_and_store_document(
        self,
        file_path: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """处理并存储文档（优化缓存清理）"""
        try:
            # 处理文档
            from .document_service import DocumentProcessor
            processor = DocumentProcessor()
            chunks = processor.process_file(file_path)

            # 添加额外元数据
            for chunk in chunks:
                chunk['metadata'].update({
                    'processed_at': datetime.now().isoformat(),
                    **(metadata or {})
                })

            # 存储到向量数据库
            ids = await self.vector_store.add_documents(chunks)

            # 清理与新文档可能相关的缓存
            filename = os.path.basename(file_path)
            await self._clear_related_cache(filename, chunks)

            # 清理临时文件
            try:
                os.remove(file_path)
            except OSError as e:
                logger.debug(f'清理临时文件失败：{file_path}, 错误：{e}')

            return {
                'success': True,
                'total_chunks': len(chunks),
                'document_ids': ids,
                'filename': os.path.basename(file_path)
            }

        except Exception as e:
            logger.error(f'处理存储文档失败：{e}')
            return {
                'success': False,
                'error': str(e),
                'filename': os.path.basename(file_path)
            }

    async def _clear_related_cache(self, filename: str, chunks: List[Dict[str, Any]]):
        """清理与文档相关的缓存"""
        try:
            cache_keys_to_delete = []
            for key in self.cache._cache.keys():
                if key.startswith('cache:rag_query:'):
                    cache_keys_to_delete.append(key)

            for key in cache_keys_to_delete:
                await self.cache.delete(key)

            logger.info(f'已清理{len(cache_keys_to_delete)}个RAG查询缓存')

        except Exception as e:
            logger.warning(f'清理相关缓存时出错：{e}')

    async def rag_query(
        self,
        query: str,
        context_count: int = 3,
    ) -> AsyncIterator[str]:
        """RAG查询（集成缓存优化）"""
        cache_key = CacheKey.rag_query(query, context_count)
        cached_result = await self.cache.get(cache_key)

        if cached_result:
            logger.info(f' RAG查询缓存命中：{query[:50]}...')
            async def cached_stream():
                yield cached_result
            return cached_stream()

        try:
            # 1. 检查相关文档
            search_results = await self.vector_store.search(
                query=query,
                k=context_count,
            )

            # 2. 构建上下文
            context = self._build_context(search_results)

            # 3. 构建prompt
            messages = self._build_messages(query, context)

            # 4. 调用大模型
            result = await self.llm.chat_completion(messages, stream=True)

            # 缓存收集与设置
            full_response_parts = []
            async for chunk in result:
                full_response_parts.append(chunk)
                yield chunk

            # 流式响应结束后，缓存完整结果
            if full_response_parts:
                full_response = ''.join(full_response_parts)
                # 仅当响应内容合理时才缓存（例如，不是错误信息）
                if len(full_response) > 10 and '抱歉，查询过程中出现错误' not in full_response:
                    await self.cache.set(cache_key, full_response, ttl=600)

        except Exception as e:
            logger.error(f'RAG查询失败：{e}')
            yield f'抱歉，查询过程中出现错误：{str(e)}'

    @staticmethod
    def _build_context(search_results: List[Dict[str, Any]]) -> str:
        """构建上下文"""
        if not search_results:
            return '没有找到相关文档内容'

        context_part = []
        for i, result in enumerate(search_results, 1):
            content = result['content']
            source = result['metadata'].get('source', '未知来源')
            context_part.append(f'[文档{i} - {source}]:\n{content}\n')

        return '\n---\n'.join(context_part)

    @staticmethod
    def _build_messages(
        query: str,
        context: str
    ) -> List[Dict[str, str]]:
        """构建消息列表"""

        system_prompt = """
        你是一个专业的AI助手，基于提供的文档内容回答问题。
        
        请遵守以下规则：
        1. 仅基于提供的上下文回答问题
        2. 如果上下文不包含相关信息，请如实说明你不知道
        3. 保持回答准确、简洁、有用
        4. 可以引用上下文中的具体内容
        
        上下文内容：
        {context}
        """

        return [
            {
                'role': 'system',
                'content': system_prompt.format(context=context)
            },
            {
                'role': 'user',
                'content': query
            }
        ]

    async def query_with_history(
        self,
        query: str,
        history: List[Dict[str, str]],
        context_count: int = 3,
    ) -> AsyncIterator[str]:
        """带历史记录的查询"""
        try:
            # 1. 检索相关文档
            search_results = await self.vector_store.search(
                query=query,
                k=context_count,
            )

            # 2. 构建上下文
            context = self._build_context(search_results)

            # 3. 构建带历史的消息
            messages = self._build_messages_with_history(query, context, history)

            # 4. 调用大模型
            result = await self.llm.chat_completion(messages, stream=True)
            async for chunk in result:
                yield chunk

        except Exception as e:
            logger.error(f'带历史查询失败：{e}')
            yield f'抱歉，查询过程中出现错误：{str(e)}'

    @staticmethod
    def _build_messages_with_history(
        query: str,
        context: str,
        history: List[Dict[str, str]],
    ) -> List[Dict[str, str]]:
        """构建带历史记录的消息"""

        system_prompt = """
        你是一个专业的AI助手，基于提供的文档内容和对话历史回答问题
        
        请遵守以下规则：
        1. 基于提供的上下文和对话历史回答问题
        2. 保持对话的连贯性
        3. 如果上下文不包含相关信息，请如实说明你不知道
        4. 保持回答准确、简洁、有用
        
        文档上下文：
        {context}
        
        对话历史：
        """

        # 添加历史记录（限制最后5轮）
        history_prompt = ''
        recent_history = history[-10:]  # 最近5轮对话
        for msg in recent_history:
            role = '用户' if msg['role'] == 'user' else '助手'
            history_prompt += f'{role}: {msg["content"]}\n'

        full_system_prompt = system_prompt.format(context=context) + history_prompt

        messages = [
            {
                'role': 'system',
                'content': full_system_prompt
            },
            {
                'role': 'user',
                'content': query
            }
        ]

        return messages


# 单例实例
_rag_service = None


def get_rag_service() -> RagService:
    """获取RAG服务单例"""
    global _rag_service
    if _rag_service is None:
        _rag_service = RagService()
    return _rag_service
