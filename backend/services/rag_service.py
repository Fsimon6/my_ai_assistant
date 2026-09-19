# -*- coding: utf-8 -*-
import os
import os
import uuid
from typing import List, Dict, Any, Optional
from collections.abc import AsyncIterator
import logging
from datetime import datetime

from backend.config.settings import settings
from backend.services.llm_service import get_llm, LLMFactory, LLMConfig
from backend.services.vector_service import get_vector_store_manager
from backend.services.document_service import DocumentProcessor, EXCEL_EXT
from backend.services.cache_service import get_cache_service, CacheKey
from backend.excel import ExcelParser
from backend.excel import store as excel_store
from backend.utils.provider_errors import (
    classify_upload_error,
    log_classified,
)

logger = logging.getLogger(__name__)

# Phase 1A 临时兼容层标记：Excel 仅写入“每 Sheet 一条摘要 chunk”，
# 用于让文档出现在现有知识库列表 / 可删除 / 可被普通 RAG 粗检索。
# 这不是最终的 Table Retrieval 架构（后续阶段将改为结构化访问）。
EXCEL_CHUNK_ROLE = 'excel_summary'
EXCEL_PHASE_TAG = '1A'
# 摘要 chunk 中内嵌的预览行数
EXCEL_SUMMARY_PREVIEW_ROWS = 5
# 单条摘要 chunk 的最大字符数（避免污染向量库）
EXCEL_SUMMARY_MAX_CHARS = 2000


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
        metadata: Optional[Dict[str, Any]] = None,
        user_id: Optional[int] = None,
        original_filename: Optional[str] = None,
        file_size: int = 0
    ) -> Dict[str, Any]:
        """处理并存储文档（优化缓存清理）

        user_id: 文档归属用户，写入每个 chunk 的 metadata，用于向量检索按用户隔离。
        original_filename: 用户上传的原始文件名，写入 metadata 供文档列表/引用展示（覆盖临时文件名）。
        file_size: 文件字节大小，写入 metadata 供文档列表展示。
        """
        # 表格文件（xlsx/xls/csv/tsv）：进入 Unified Table Representation 链路
        file_ext = os.path.splitext(file_path)[1].lower()
        if file_ext in EXCEL_EXT:
            return await self._process_excel_document(
                file_path=file_path,
                metadata=metadata,
                user_id=user_id,
                original_filename=original_filename,
                file_size=file_size,
            )

        try:
            # 处理文档
            from backend.services.document_service import DocumentProcessor
            processor = DocumentProcessor()
            chunks = processor.process_file(file_path)

            # 生成单文档级 id（覆盖本次上传的所有 chunk，用于“文档 → 向量”生命周期管理）
            document_id = uuid.uuid4().hex

            # 添加额外元数据（含 document_id 与归属用户）
            for chunk in chunks:
                chunk['metadata'].update({
                    'document_id': document_id,
                    'processed_at': datetime.now().isoformat(),
                    **(metadata or {})
                })
                # 服务端强制写入：原始文件名（source 用于引用、filename 用于列表）、大小、归属用户，
                # 必须置于 **(metadata) 之后，确保不被客户端 metadata 覆盖。
                if original_filename:
                    chunk['metadata']['source'] = original_filename
                    chunk['metadata']['filename'] = original_filename
                if file_size:
                    chunk['metadata']['file_size'] = file_size
                if user_id is not None:
                    chunk['metadata']['user_id'] = user_id
                # 以 document_id 命名空间 chunk id，便于按文档精确删除
                chunk_index = chunk['metadata'].get('chunk_index', 0)
                chunk['id'] = f'{document_id}_{chunk_index}'

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
                'document_id': document_id,
                'chunk_ids': ids,
                'filename': filename
            }

        except Exception as e:
            # 失败清理：embedding/向量写入失败时已落盘的临时文件即为半成品，
            # 需删除避免孤儿文件；向量侧按 document_id 尽力清理。
            document_id = locals().get('document_id') or ''
            if document_id:
                await self._cleanup_failed_upload(document_id, file_path, user_id)
            else:
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                except OSError as oe:
                    logger.debug(f'清理失败临时文件出错：{file_path}, {oe}')
            classified = classify_upload_error(e)
            log_classified('document_upload', classified)
            return {
                'success': False,
                'error': classified.detail or str(e),
                'error_code': classified.error_code,
                'message': classified.message,
                'http_status': classified.http_status,
                'filename': os.path.basename(file_path)
            }

    async def _process_excel_document(
        self,
        file_path: str,
        metadata: Optional[Dict[str, Any]] = None,
        user_id: Optional[int] = None,
        original_filename: Optional[str] = None,
        file_size: int = 0,
    ) -> Dict[str, Any]:
        """Phase 1A：Excel 上传处理链路。

        流程：原文件 -> ExcelParser -> Unified Representation -> 落盘（原文件 + json）
              -> 生成“每 Sheet 一条摘要 chunk”写入 Chroma（临时兼容层）
              -> 删除临时文件
        """
        document_id = uuid.uuid4().hex
        display_name = original_filename or os.path.basename(file_path)
        try:
            parser = ExcelParser()
            representation = parser.parse(
                file_path=file_path,
                document_id=document_id,
                user_id=user_id,
                filename=display_name,
                file_type=os.path.splitext(file_path)[1].lstrip('.').lower(),
            )

            # 1) 持久化原始文件 + representation（后续阶段复用；Preview 只读 representation）
            excel_store.save_artifacts(representation, file_path, display_name)

            # 2) Phase 1A 临时兼容层：仅写摘要 chunk，不把全部数据塞进向量库
            chunks = self._build_excel_chunks(
                representation=representation,
                metadata=metadata,
                original_filename=display_name,
                file_size=file_size,
                user_id=user_id,
            )
            ids = await self.vector_store.add_documents(chunks) if chunks else []

            # 3) 清理与新文档相关的缓存
            await self._clear_related_cache(display_name, chunks)

            # 4) 删除临时上传文件（原文件已持久化到 data/excel/<document_id>/）
            try:
                os.remove(file_path)
            except OSError as e:
                logger.debug('清理临时表格文件失败：%s, 错误：%s', file_path, e)

            logger.info(
                'Excel 处理完成 document_id=%s sheets=%d rows=%d chunks=%d',
                document_id, representation.sheet_count,
                representation.total_rows, len(chunks),
            )
            return {
                'success': True,
                'doc_kind': 'excel',
                'total_chunks': len(chunks),
                'document_id': document_id,
                'chunk_ids': ids,
                'filename': display_name,
                'file_type': representation.file_type,
                'sheet_count': representation.sheet_count,
                'total_rows': representation.total_rows,
                'sheets': representation.sheets_meta(),
                'parse_ms': representation.parse_ms,
                'warnings': list(representation.warnings),
            }

        except Exception as e:
            # 失败清理：移除半成品附件目录 / 临时文件 / 可能已写入的向量，
            # 保证"失败即无残留"，用户不会看到一个查不到的幽灵文档。
            await self._cleanup_failed_upload(document_id, file_path, user_id)
            classified = classify_upload_error(e)
            log_classified('excel_upload', classified)
            return {
                'success': False,
                'error': classified.detail or str(e),
                'error_code': classified.error_code,
                'message': classified.message,
                'http_status': classified.http_status,
                'filename': os.path.basename(file_path),
            }

    async def _cleanup_failed_upload(
        self,
        document_id: str,
        file_path: str,
        user_id: Optional[int],
    ) -> None:
        """上传失败后的**尽力清理**：附件目录 + 临时文件 + 已写入的向量。

        任何一步失败都只记日志，不影响把错误如实返回给用户。
        """
        try:
            excel_store.delete_artifacts(document_id)
        except Exception as de:
            logger.warning('清理 Excel 附件目录失败（忽略）：%s', de)
        try:
            if getattr(self, 'vector_store', None) is not None:
                deleted = await self.vector_store.delete_documents([document_id], user_id=user_id)
                if deleted:
                    logger.info('已清理失败上传残留向量：document_id=%s count=%s', document_id, deleted)
        except Exception as ve:
            logger.warning('清理失败上传残留向量出错（忽略）：%s', ve)
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except OSError as oe:
            logger.debug('清理失败临时文件出错：%s, %s', file_path, oe)

    def _build_excel_chunks(
        self,
        representation,
        metadata: Optional[Dict[str, Any]],
        original_filename: Optional[str],
        file_size: int,
        user_id: Optional[int],
    ) -> List[Dict[str, Any]]:
        """生成 Excel 摘要 chunk（Phase 1A 临时兼容层，每 Sheet 一条）。"""
        chunks: List[Dict[str, Any]] = []
        processed_at = datetime.now().isoformat()
        sheet_names = ','.join(s.sheet_name for s in representation.sheets)

        for sheet in representation.sheets:
            preview_lines = []
            header = sheet.column_names
            preview_lines.append(' | '.join(header))
            for row in sheet.rows[:EXCEL_SUMMARY_PREVIEW_ROWS]:
                preview_lines.append(' | '.join('' if v is None else str(v) for v in row))

            content = (
                f'[Excel 表格摘要] 文件：{representation.filename}\n'
                f'Sheet：{sheet.sheet_name}（第 {sheet.sheet_index + 1}/{representation.sheet_count} 个）\n'
                f'规模：{sheet.row_count} 行 × {sheet.column_count} 列\n'
                f'列名（{sheet.column_count}）：{", ".join(header)}\n'
                f'前 {min(EXCEL_SUMMARY_PREVIEW_ROWS, sheet.row_count)} 行预览：\n'
                + '\n'.join(preview_lines)
            )[:EXCEL_SUMMARY_MAX_CHARS]

            meta: Dict[str, Any] = {
                'source': original_filename,
                'filename': original_filename,
                'chunk_index': sheet.sheet_index,
                'document_id': representation.document_id,
                'processed_at': processed_at,
                'doc_kind': 'excel',
                'excel_phase': EXCEL_PHASE_TAG,
                'chunk_role': EXCEL_CHUNK_ROLE,
                'sheet_name': sheet.sheet_name,
                'sheet_index': sheet.sheet_index,
                'sheet_count': representation.sheet_count,
                'file_type': representation.file_type,
                'columns': ','.join(header),
                'row_count': sheet.row_count,
                'column_count': sheet.column_count,
            }
            if file_size:
                meta['file_size'] = file_size
            if user_id is not None:
                meta['user_id'] = user_id
            if metadata:
                # 客户端 metadata 最后合并，但不允许覆盖服务端强字段
                for k, v in metadata.items():
                    if k not in meta:
                        meta[k] = v

            chunks.append({
                'id': f'{representation.document_id}_{sheet.sheet_index}',
                'content': content,
                'metadata': meta,
            })

        return chunks

    async def _clear_related_cache(self, filename: str, chunks: List[Dict[str, Any]]):
        """清理与文档相关的缓存（缓存键为 md5，无法按前缀匹配，直接清空全局 RAG 缓存）"""
        try:
            await self.cache.clear()
            logger.info('已清理全部 RAG 查询缓存')
        except Exception as e:
            logger.warning(f'清理相关缓存时出错：{e}')

    def _resolve_llm(self, model: Optional[str], api_key: Optional[str]) -> 'BaseLLM':
        """解析用于本次生成的 LLM 实例（与 /speak/stream 行为一致）：
        - 同时提供 model 与 api_key → 角色专属配置
        - 否则 → 全局默认实例 self.llm
        """
        if model and api_key:
            logger.info(f'RAG 使用角色专属模型：{model}')
            return LLMFactory.create_llm(LLMConfig(
                provider=settings.LLM_PROVIDER,
                api_key=api_key,
                base_url=settings.LLM_BASE_URL,
                model=model,
                embedding_model=settings.EMBEDDING_MODEL,
                temperature=float(os.getenv('LLM_TEMPERATURE', '0.7')),
                max_tokens=int(os.getenv('LLM_MAX_TOKENS', '2000')),
            ))
        logger.info(f'RAG 使用全局默认模型：{settings.LLM_MODEL}')
        return self.llm

    async def rag_query(
        self,
        query: str,
        context_count: int = 3,
        stream: bool = False,
        user_id: Optional[int] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        document_id: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """RAG查询（集成缓存优化）

        user_id: 用于缓存键隔离 + 向量检索按用户过滤，避免跨用户数据泄露。
        model/api_key: 角色级配置（两者都填才生效），用于覆盖全局默认模型。
        document_id: 可选，限定只在该文档内检索（按文档查询/预览场景）。
        """
        # 按文档检索不走全局缓存，避免不同文档的查询结果被同一 cache key 污染
        use_cache = document_id is None
        cache_key = CacheKey.rag_query(query, context_count, user_id) if use_cache else None
        if use_cache:
            cached_result = await self.cache.get(cache_key)
            if cached_result:
                logger.info(f' RAG查询缓存命中：{query[:50]}...')
                yield cached_result
                return

        try:
            # 1. 检索相关文档（按归属用户 + 可选 document_id 过滤）
            # Chroma where 仅支持单操作符；多条件用 $and 包裹
            conds: List[Dict[str, Any]] = []
            if user_id is not None:
                conds.append({'user_id': user_id})
            if document_id:
                conds.append({'document_id': document_id})
            filter_dict: Optional[Dict[str, Any]] = (
                {'$and': conds} if len(conds) > 1 else (conds[0] if conds else None)
            )
            search_results = await self.vector_store.search(
                query=query,
                k=context_count,
                filter_dict=filter_dict
            )

            # 2. 构建上下文
            context = self._build_context(search_results)

            # 3. 构建prompt
            messages = self._build_messages(query, context)

            # 4. 选择 LLM：角色专属或全局默认，随后调用大模型（chat_completion 始终返回异步生成器）
            llm = self._resolve_llm(model, api_key)
            full_response_parts = []
            async for chunk in llm.chat_completion(messages, stream=True):
                full_response_parts.append(chunk)
                yield chunk

            # 流式响应结束后，缓存完整结果（仅全局检索走缓存）
            if use_cache and full_response_parts:
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
        stream: bool = False,
        user_id: Optional[int] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        document_id: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """带历史记录的查询（按归属用户过滤向量检索）

        model/api_key: 角色级配置（两者都填才生效），用于覆盖全局默认模型。
        document_id: 可选，限定只在该文档内检索。
        """
        try:
            # 1. 检索相关文档（按归属用户 + 可选 document_id 过滤）
            # Chroma where 仅支持单操作符；多条件用 $and 包裹
            conds: List[Dict[str, Any]] = []
            if user_id is not None:
                conds.append({'user_id': user_id})
            if document_id:
                conds.append({'document_id': document_id})
            filter_dict: Optional[Dict[str, Any]] = (
                {'$and': conds} if len(conds) > 1 else (conds[0] if conds else None)
            )
            search_results = await self.vector_store.search(
                query=query,
                k=context_count,
                filter_dict=filter_dict
            )

            # 2. 构建上下文
            context = self._build_context(search_results)

            # 3. 构建带历史的消息
            messages = self._build_messages_with_history(query, context, history)

            # 4. 选择 LLM：角色专属或全局默认，随后调用大模型（chat_completion 始终返回异步生成器）
            llm = self._resolve_llm(model, api_key)
            async for chunk in llm.chat_completion(messages, stream=True):
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
