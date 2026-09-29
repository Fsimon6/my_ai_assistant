from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
import json
import os
import logging
from datetime import date, datetime
from decimal import Decimal

from backend.config.settings import settings
from backend.services.rag_service import get_rag_service
from backend.services.conversation_service import (
    conversation_service, SOURCE_RAG, SOURCE_EXCEL,
)
from backend.services.document_service import save_uploaded_file
from backend.excel import store as excel_store
from backend.utils.auth import get_current_active_user
from backend.utils.provider_errors import (
    classify_llm_error,
    classify_upload_error,
    log_classified,
)
from backend.models.user import User


router = APIRouter(prefix='/api/v1/rag', tags=['RAG'])
logger = logging.getLogger(__name__)


class QueryRequest(BaseModel):
    """查询请求体（与前端 ragApi.queryDocument POST 体一致）"""
    query: str
    stream: bool = False
    context_count: int = 3
    character_id: Optional[str] = None  # 可选：传入则使用角色专属 model+api_key（与 /speak/stream 一致）
    document_id: Optional[str] = None  # 可选：限定只在该文档内检索（按文档查询/预览）


class QueryWithHistoryRequest(BaseModel):
    """带历史查询请求体（与前端 ragApi.queryWithHistory POST 体一致）"""
    query: str
    history: List[dict] = []
    stream: bool = False
    context_count: int = 3
    character_id: Optional[str] = None  # 可选：传入则使用角色专属 model+api_key
    document_id: Optional[str] = None  # 可选：限定只在该文档内检索


@router.post('/upload')
async def upload_document(
    file: UploadFile = File(...),
    metadata: Optional[str] = None,
    current_user: User = Depends(get_current_active_user)
):
    """上传并处理文档（需登录；文档归属当前用户）"""
    try:
        rag_service = get_rag_service()

        # 验证文件类型（普通文档 + 表格文件）
        allowed_types = ['.pdf', '.txt', '.docx', '.md', '.xlsx', '.xls', '.csv', '.tsv']
        file_ext = '.' + file.filename.split('.')[-1] if '.' in file.filename else ''

        if file_ext.lower() not in allowed_types:
            raise HTTPException(
                status_code=400,
                detail=f'不支持的文件类型。支持的类型：{",".join(allowed_types)}'
            )

        # 保存文件
        file_path = await save_uploaded_file(file)

        # 原始文件名与大小（用于文档列表展示，服务端强制写入 metadata，避免只存临时名）
        original_filename = file.filename
        file_size = 0
        try:
            file_size = os.path.getsize(file_path)
        except OSError:
            file_size = 0

        # 解析元数据
        parsed_metadata = {}
        if metadata:
            try:
                parsed_metadata = json.loads(metadata)
            except:
                parsed_metadata = {'custom_metadata': metadata}

        # 处理文档（归属当前用户，用于向量隔离）
        result = await rag_service.process_and_store_document(
            file_path,
            parsed_metadata,
            user_id=current_user.id,
            original_filename=original_filename,
            file_size=file_size
        )

        if result['success']:
            response = {
                'success': True,
                'message': '文档处理成功',
                'document_id': result['document_id'],
                'chunk_ids': result.get('chunk_ids'),
                'filename': result['filename'],
                'total_chunks': result['total_chunks'],
                'doc_kind': result.get('doc_kind', 'document'),
            }
            # 表格文件（Phase 1A）附加 Representation 摘要信息
            if result.get('doc_kind') == 'excel':
                response.update({
                    'file_type': result.get('file_type'),
                    'sheet_count': result.get('sheet_count'),
                    'total_rows': result.get('total_rows'),
                    'sheets': result.get('sheets'),
                    'parse_ms': result.get('parse_ms'),
                    'warnings': result.get('warnings'),
                })
            return response
        else:
            # 稳定性补丁：不再统一 500 + 原始 provider 文本。
            # rag_service 已把失败分类为 (error_code, message, http_status)，
            # 这里原样透出，前端据此展示可理解的原因（不泄露密钥/provider 细节）。
            classified = classify_upload_error(result.get('error'))
            error_code = result.get('error_code') or classified.error_code
            message = result.get('message') or classified.message
            status = int(result.get('http_status') or classified.http_status or 500)
            logger.warning(
                '上传失败：error_code=%s filename=%s detail=%s',
                error_code, original_filename, classified.detail,
            )
            raise HTTPException(
                status_code=status,
                detail={'error_code': error_code, 'message': message},
            )

    except HTTPException:
        raise
    except Exception as e:
        # 兜底：走同一套分类，绝不把 provider 原始内容直接回给前端
        classified = classify_upload_error(e)
        logger.error('上传文档失败：error_code=%s detail=%s',
                     classified.error_code, classified.detail)
        raise HTTPException(
            status_code=classified.http_status,
            detail=classified.to_response(),
        )


@router.post('/query')
async def query_document(
    req: QueryRequest,
    current_user: User = Depends(get_current_active_user)
):
    """查询文档（需登录；按当前用户隔离向量检索与缓存）"""
    try:
        rag_service = get_rag_service()

        # 解析角色级模型（可选）：传入 character_id 时按归属用户取出 model+api_key 覆盖全局默认
        char_model, char_api_key = None, None
        if req.character_id:
            from backend.services.character_service import character_service
            character = character_service.get_character(req.character_id, current_user.id)
            if character is None:
                raise HTTPException(status_code=404, detail='角色不存在')
            char_model = character.model
            char_api_key = character.api_key

        if req.stream:
            async def generate():
                full_response = ''
                async for chunk in rag_service.rag_query(
                    query=req.query,
                    context_count=req.context_count,
                    stream=True,
                    user_id=current_user.id,
                    model=char_model,
                    api_key=char_api_key,
                    document_id=req.document_id
                ):
                    full_response += chunk
                    yield json.dumps({
                        'type': 'chunk',
                        'content': chunk,
                        'timestamp': datetime.now().isoformat()
                    }) + '\n'

                # 与普通 Chat 流式协议保持一致：末尾补 complete 帧
                yield json.dumps({
                    'type': 'complete',
                    'content': full_response,
                    'timestamp': datetime.now().isoformat()
                }) + '\n'

            return StreamingResponse(
                generate(),
                media_type='application/x-ndjson',
                headers={
                    'Cache-Control': 'no-cache',
                    'X-Accel-Buffering': 'no'
                }
            )
        else:
            response_text = ''
            async for chunk in rag_service.rag_query(
                query=req.query,
                context_count=req.context_count,
                stream=False,
                user_id=current_user.id,
                model=char_model,
                api_key=char_api_key,
                document_id=req.document_id
            ):
                response_text += chunk

            return {
                'success': True,
                'response': response_text,
                'query': req.query,
                'timestamp': datetime.now().isoformat()
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'查询失败：{e}')
        raise HTTPException(
            status_code=500,
            detail=f'查询失败：{str(e)}'
        )


def _resolve_rag_conversation_id(character_id: Optional[str], user_id: int) -> Optional[int]:
    """RAG 链路定位（或创建）当前对话 id，供聊天记录持久化复用。

    与 ``main.py:/speak/stream`` 使用同一个 conversation_service，不复制业务逻辑。
    - ``character_id`` 缺失（None / 空串）→ 返回 None：调用方跳过持久化，
      保持本端点原有的“无副作用”行为，向后兼容直接调用 API 的场景。
    - ``character_id`` 非法（非整数）→ 返回 None，不抛错。
    - 数据库侧异常 → 记日志后返回 None（保存失败绝不能影响本次查询响应）。
    """
    if not character_id:
        return None
    try:
        character_id_int = int(character_id)
    except (TypeError, ValueError):
        logger.warning('RAG 对话记录：character_id 非法，跳过持久化：%r', character_id)
        return None
    try:
        return conversation_service.get_or_create_conversation_id(user_id, character_id_int)
    except Exception as e:
        logger.warning('RAG 对话记录初始化失败（忽略）：%s', e)
        return None


@router.post('/query-with-history')
async def query_with_history(
        req: QueryWithHistoryRequest,
        current_user: User = Depends(get_current_active_user)
):
    """带历史记录的查询（需登录；按当前用户隔离向量检索）"""
    try:
        rag_service = get_rag_service()

        # 解析角色级模型（可选）：传入 character_id 时按归属用户取出 model+api_key 覆盖全局默认
        char_model, char_api_key = None, None
        if req.character_id:
            from backend.services.character_service import character_service
            character = character_service.get_character(req.character_id, current_user.id)
            if character is None:
                raise HTTPException(status_code=404, detail='角色不存在')
            char_model = character.model
            char_api_key = character.api_key

        # ===== 对话历史持久化（与 main.py:/speak/stream 同一策略）=====
        # 背景：本端点是「启用知识库」后的聊天入口，此前完全没有落库逻辑，
        # 导致开启知识库后聊天记录无法保存。此处只复用 conversation_service：
        #   * 流式开始前写入 user 消息；
        #   * 流式完整结束后写入一次 assistant 消息（不按 chunk 写库）；
        #   * 异常时补写失败占位，保持 user/assistant 对称。
        # character_id 缺失时 conv_id 为 None → 全部跳过，保持旧行为（向后兼容）。
        # 不改变任何请求/响应契约；持久化失败只记日志，绝不影响查询响应。
        conv_id = _resolve_rag_conversation_id(req.character_id, current_user.id)
        effective_model = char_model if (char_model and char_api_key) else settings.LLM_MODEL

        def _persist_message(role: str, content: str) -> None:
            """写入一条对话记录（来源标记 = 知识库）；
            任何失败只记日志，绝不冒泡影响本次查询。"""
            if conv_id is None or not content:
                return
            try:
                conversation_service.append_message(
                    conv_id, role, content, effective_model,
                    meta_info={'source': SOURCE_RAG},
                )
            except Exception as pe:
                logger.warning('保存 RAG 对话记录失败（忽略）：%s', pe)

        if req.stream:
            async def generate():
                full_response = ''
                # 流式开始前先落库用户消息
                _persist_message('user', req.query)
                try:
                    async for chunk in rag_service.query_with_history(
                        query=req.query,
                        history=req.history,
                        context_count=req.context_count,
                        user_id=current_user.id,
                        model=char_model,
                        api_key=char_api_key,
                        document_id=req.document_id
                    ):
                        full_response += chunk
                        yield json.dumps({
                            'type': 'chunk',
                            'content': chunk,
                            'timestamp': datetime.now().isoformat()
                        }) + '\n'

                    # 流式完整结束后写一次 AI 回复（不按 chunk 写库）
                    _persist_message('assistant', full_response)

                    # 与普通 Chat 流式协议保持一致：末尾补 complete 帧（携带完整内容）
                    yield json.dumps({
                        'type': 'complete',
                        'content': full_response,
                        'timestamp': datetime.now().isoformat()
                    }) + '\n'
                except Exception as se:
                    logger.error(f'RAG 流式查询失败：{se}')
                    # 与 /speak/stream 一致：异常时补一条失败助手消息，保持历史对称
                    _persist_message('assistant', '（内容生成失败，请重试）')
                    raise

            return StreamingResponse(
                generate(),
                media_type='application/x-ndjson',
                headers={
                    'Cache-Control': 'no-cache',
                    'X-Accel-Buffering': 'no'
                }
            )
        else:
            response_text = ''
            _persist_message('user', req.query)
            try:
                async for chunk in rag_service.query_with_history(
                    query=req.query,
                    history=req.history,
                    context_count=req.context_count,
                    user_id=current_user.id,
                    model=char_model,
                    api_key=char_api_key,
                    document_id=req.document_id
                ):
                    response_text += chunk
                _persist_message('assistant', response_text)
            except Exception:
                # 异常同样补失败占位（与外层 500 处理各司其职：这里只补历史对称）
                _persist_message('assistant', '（内容生成失败，请重试）')
                raise

            return {
                'success': True,
                'response': response_text,
                'query': req.query,
                'timestamp': datetime.now().isoformat()
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'带历史查询失败：{e}')
        raise HTTPException(
            status_code=500,
            detail=f'查询失败：{str(e)}'
        )


@router.get('/collection-info')
async def get_collection_info(
    current_user: User = Depends(get_current_active_user)
):
    """获取向量数据库信息（需登录）"""
    try:
        from backend.services.vector_service import get_vector_store_manager
        vector_store = get_vector_store_manager()
        info = vector_store.get_collection_info()

        return {
            'success': True,
            'collection_info': info,
            'timestamp': datetime.now().isoformat()
        }

    except Exception as e:
        logger.error(f'获取集合信息失败：{e}')
        raise HTTPException(
            status_code=500,
            detail=f'获取集合信息失败：{str(e)}'
        )


class DeleteDocumentsRequest(BaseModel):
    """删除文档请求体（DELETE 携带 JSON 体）"""
    document_ids: List[str]


@router.delete('/documents')
async def delete_documents(
    req: DeleteDocumentsRequest,
    current_user: User = Depends(get_current_active_user)
):
    """删除文档（需登录；仅删除归属当前用户的文档）"""
    try:
        from backend.services.vector_service import get_vector_store_manager
        vector_store = get_vector_store_manager()
        deleted_count = await vector_store.delete_documents(req.document_ids, user_id=current_user.id)

        # 同步清理归属于当前用户的 Excel 附件（原文件 + representation），避免孤儿文件
        cleaned_artifacts = 0
        for doc_id in req.document_ids:
            if excel_store.load_representation(doc_id, user_id=current_user.id) is not None:
                if excel_store.delete_artifacts(doc_id):
                    cleaned_artifacts += 1

        return {
            'success': True,
            'message': '文档删除成功' if deleted_count else '未删除任何归属于当前用户的文档',
            'delete_ids': req.document_ids,
            'deleted_count': deleted_count,
            'deleted_artifacts': cleaned_artifacts,
            'timestamp': datetime.now().isoformat()
        }

    except Exception as e:
        logger.error(f'删除文档失败：{e}')
        raise HTTPException(
            status_code=500,
            detail=f'删除文档失败“：{str(e)}'
        )


@router.get('/documents')
async def list_documents(
    current_user: User = Depends(get_current_active_user)
):
    """列出当前用户的文档（需登录；按 user_id 隔离，聚合自 Chroma 向量 metadata）

    返回字段（仅 UI 实际需要的）：document_id / filename / type / size / chunks / created_at
    """
    try:
        from backend.services.vector_service import get_vector_store_manager
        vector_store = get_vector_store_manager()
        docs = vector_store.list_user_documents(user_id=current_user.id)
        return {
            'success': True,
            'documents': docs,
            'total': len(docs),
            'timestamp': datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f'列出文档失败：{e}')
        raise HTTPException(
            status_code=500,
            detail=f'列出文档失败：{str(e)}'
        )


@router.get('/documents/{document_id}')
async def get_document(
    document_id: str,
    current_user: User = Depends(get_current_active_user)
):
    """获取单个文档的全部分块内容（需登录；仅返回归属当前用户的文档，供预览/查看原文）

    无需新增数据库表，直接基于 Chroma 向量（已含 document_id + user_id + chunk_index）。
    """
    try:
        from backend.services.vector_service import get_vector_store_manager
        vector_store = get_vector_store_manager()
        chunks = await vector_store.get_document_chunks(document_id, user_id=current_user.id)
        if not chunks:
            raise HTTPException(status_code=404, detail='文档不存在或无权访问')
        return {
            'success': True,
            'document_id': document_id,
            'chunks': chunks,
            'total': len(chunks),
            'timestamp': datetime.now().isoformat()
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'获取文档内容失败：{e}')
        raise HTTPException(
            status_code=500,
            detail=f'获取文档内容失败：{str(e)}'
        )


@router.get('/excel/{document_id}/preview')
async def get_excel_preview(
    document_id: str,
    sheet_index: int = 0,
    limit: int = 20,
    current_user: User = Depends(get_current_active_user)
):
    """Excel 基础预览（Phase 1A）

    - 数据来源：上传时落盘的 Unified Representation（representation.json），
      绝不在预览时重新解析原始文件 / 不重新 read_excel；
    - 用户隔离：按 document_id 读取后强制校验 representation.user_id == 当前用户，
      不匹配一律返回 404（不泄露文档是否存在）；
    - 返回：文件信息 + Sheet 列表 + 当前 Sheet 的列名 + 前 limit 行数据。
    """
    # 参数收敛，避免异常入参
    limit = max(1, min(int(limit), 200))
    try:
        representation = excel_store.load_representation(document_id, user_id=current_user.id)
        if representation is None:
            raise HTTPException(status_code=404, detail='表格文档不存在或无权访问')

        if sheet_index < 0 or sheet_index >= representation.sheet_count:
            sheet_index = 0

        sheet = representation.sheet_by_index(sheet_index)
        if sheet is None:
            raise HTTPException(status_code=404, detail='Sheet 不存在')

        preview_rows = sheet.rows[:limit]
        return {
            'success': True,
            'document_id': representation.document_id,
            'filename': representation.filename,
            'file_type': representation.file_type,
            'parser': representation.parser,
            'sheet_count': representation.sheet_count,
            'total_rows': representation.total_rows,
            'parse_ms': representation.parse_ms,
            'sheets': representation.sheets_meta(),
            'active_sheet': {
                'sheet_index': sheet.sheet_index,
                'sheet_name': sheet.sheet_name,
                'row_count': sheet.row_count,
                'column_count': sheet.column_count,
                'header_mode': sheet.header_mode,
                'header_rows_excel': sheet.header_rows_excel,
                'header_depth': sheet.header_depth,
                'excel_range': sheet.excel_range,
                'columns': [c.to_dict() for c in sheet.columns],
                'column_names': sheet.column_names,
                'rows': preview_rows,
                'row_excel_numbers': sheet.row_excel_numbers[:len(preview_rows)],
                'preview_limit': limit,
                'preview_count': len(preview_rows),
                'truncated': sheet.row_count > len(preview_rows),
                'warnings': sheet.warnings,
            },
            'warnings': representation.warnings,
            'timestamp': datetime.now().isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'Excel 预览失败：{e}')
        raise HTTPException(status_code=500, detail=f'Excel 预览失败：{str(e)}')


class ExcelFilterRequest(BaseModel):
    """单个筛选条件（Phase 1B + Phase 2）

    operator 白名单：eq | neq | gt | gte | lt | lte | contains
    （gt/gte/lt/lte 需要数值；contains 需要非空字符串）
    """
    column: str
    operator: str = 'eq'
    value: Optional[Any] = None


class ExcelQueryRequestBody(BaseModel):
    """结构化查询请求体（Phase 1B + Phase 2）

    - 数据源为 representation.json（非 Chroma、非原始 Excel 重解析）
    - sheet_index 与 sheet_name 同时提供且冲突时返回 400
    - match_mode 本轮仅支持 'and'
    - engine（Phase 2）：auto（默认，优先 DuckDB）| duckdb | python。
      仅供排障与差分验证使用；SQL 始终由 Python 生成，不接受任何 SQL 文本入参。
    """
    sheet_index: Optional[int] = None
    sheet_name: Optional[str] = None
    columns: Optional[List[str]] = None
    filters: List[ExcelFilterRequest] = []
    match_mode: str = 'and'
    limit: int = 50
    offset: int = 0
    engine: str = 'auto'


@router.post('/excel/{document_id}/query')
async def query_excel_document(
    document_id: str,
    req: ExcelQueryRequestBody,
    current_user: User = Depends(get_current_active_user)
):
    """Excel 结构化查询（Phase 1B + Phase 2）

    支持：Sheet 定位 / 单列多列投影 / eq 等值 / neq 不等 / gt·gte·lt·lte 数值范围 /
         contains 包含 / AND 多条件 / limit-offset 分页 / Excel 原始行号与 Range 来源。

    执行引擎：默认 DuckDB（Python 构造受控 SQL，值全部参数绑定）；
             不可用时回退到 Phase 1B 的 Python 引擎。
    不涉及：LLM、Chroma、统计聚合（COUNT/SUM/GROUP BY/SORT 均不支持）。
    用户隔离：按 document_id 读取 representation 后强制校验 user_id，不匹配返回 404。
    """
    from backend.excel import engine as excel_engine
    from backend.excel.query import ExcelQueryError

    try:
        representation = excel_store.load_representation(document_id, user_id=current_user.id)
        if representation is None:
            raise HTTPException(status_code=404, detail='表格文档不存在或无权访问')

        payload = req.model_dump() if hasattr(req, 'model_dump') else req.dict()
        engine_name = payload.pop('engine', 'auto')
        result, engine_used = excel_engine.run_structured_query(
            representation, payload, engine=engine_name
        )

        return {
            'success': True,
            'filename': representation.filename,
            'file_type': representation.file_type,
            'engine': engine_used,
            **result.to_dict(),
            'timestamp': datetime.now().isoformat(),
        }
    except HTTPException:
        raise
    except ExcelQueryError as e:
        # 领域错误：明确 code + 可读 message + 结构化 details（供前端消歧/提示）
        raise HTTPException(status_code=e.status, detail=e.to_dict())
    except Exception as e:
        logger.error(f'Excel 结构化查询失败：{e}')
        raise HTTPException(status_code=500, detail=f'Excel 结构化查询失败：{str(e)}')


class ExcelAggregateRequestBody(BaseModel):
    """统计请求体（Phase 3A）

    operation: count | sum | avg | min | max
    - count 时 column 必须为空（COUNT(*) = 命中行数；不实现 COUNT(DISTINCT)）；
    - sum/avg/min/max 必须给 column，且该列必须可数值化（否则明确报错）。
    group_by（Phase 3B）：分组字段数组（最多 3 列）。
    - 为空 -> 单值统计（Phase 3A 语义）；
    - 非空 -> 分组统计（GROUP BY），返回每个分组一行。
    order_by / order_dir / top_n（Phase 3C）：排序 + TOP-N，只作用于分组结果。
    - order_by：'aggregate_value'（按统计值）或某个分组列名（固定枚举，非法值直接 400）；
    - order_dir：'asc' | 'desc'（缺省：聚合值->desc，分组字段->asc）；
    - top_n：1~200（**必须有排序依据**，否则 400）；LIMIT 在 GROUP BY/ORDER BY 之后生效；
    - 排序键相同的分组由 Python 自动追加分组字段升序作为 tie-breaker（结果稳定可复现）。
    filters 与 Phase 2 完全一致（eq/neq/gt/gte/lt/lte/contains，多条件 AND）。
    engine 仅供排障/差分验证：auto（默认，优先 DuckDB）| duckdb | python。
    """
    sheet_index: Optional[int] = None
    sheet_name: Optional[str] = None
    operation: str = 'count'
    column: Optional[str] = None
    group_by: List[str] = []
    order_by: Optional[str] = None
    order_dir: Optional[str] = None
    top_n: Optional[int] = None
    filters: List[ExcelFilterRequest] = []
    match_mode: str = 'and'
    engine: str = 'auto'


@router.post('/excel/{document_id}/aggregate')
async def aggregate_excel_document(
    document_id: str,
    req: ExcelAggregateRequestBody,
    current_user: User = Depends(get_current_active_user)
):
    """Excel 基础统计（Phase 3A）：COUNT / SUM / AVG / MIN / MAX

    - 统计值 100% 由 DuckDB 计算（Python 构造受控 SELECT；筛选值全部参数绑定）；
    - 空值与非数值单元格不计入，也不当作 0；命中行无有效数值时返回 value=null 并说明；
    - 目标列整表都不可数值化 -> 明确报错（绝不返回看起来像数字的错值）；
    - 返回完整来源信息：filename / sheet / operation / column / filters / matched_rows /
      匹配的 Excel 行号与区间。
    用户隔离：按 document_id 读取 representation 后强制校验 user_id，不匹配返回 404。
    """
    from backend.excel import engine as excel_engine
    from backend.excel.query import ExcelQueryError

    try:
        representation = excel_store.load_representation(document_id, user_id=current_user.id)
        if representation is None:
            raise HTTPException(status_code=404, detail='表格文档不存在或无权访问')

        payload = req.model_dump() if hasattr(req, 'model_dump') else req.dict()
        engine_name = payload.pop('engine', 'auto')
        result, engine_used = excel_engine.run_aggregate(
            representation, payload, engine=engine_name
        )

        return {
            'success': True,
            'filename': representation.filename,
            'file_type': representation.file_type,
            'engine': engine_used,
            **result.to_dict(),
            'timestamp': datetime.now().isoformat(),
        }
    except HTTPException:
        raise
    except ExcelQueryError as e:
        raise HTTPException(status_code=e.status, detail=e.to_dict())
    except Exception as e:
        logger.error(f'Excel 统计失败：{e}')
        raise HTTPException(status_code=500, detail=f'Excel 统计失败：{str(e)}')


# ============================================================================
# 第 3 项「结果与可解释性」：Excel 历史快照（assistant 消息的 meta_info['excel']）
# ============================================================================
#: 历史快照 schema 版本：前端按版本号决定能否恢复卡片；未知版本 -> 回退纯文字展示
EXCEL_HISTORY_SCHEMA_VERSION = 1

#: 历史快照里每类结果保留的最大行数（与 NL 查询的 limit 上限一致，不额外凭空收紧）。
#: 超出时截断并置 history_truncated=True，前端必须显式提示，绝不假装结果完整。
EXCEL_HISTORY_MAX_ROWS = 500

#: 相邻 P2（2026-09-29）：``matched_row_runs`` 系列是**可选增量字段**（全部命中行的真实
#: 最大连续段），旧快照缺字段时前端不显示该行；schema_version 保持 1，无 migration、不回填。
_RESULT_KEYS = ('offset', 'limit', 'returned_count', 'total_matches', 'total_rows_in_sheet',
                'has_more', 'next_offset', 'sheet_index', 'sheet_name',
                'matched_row_runs', 'matched_row_run_count', 'matched_row_runs_truncated')
_AGG_KEYS = ('operation', 'operation_label', 'column', 'column_index', 'column_letter',
             'column_numeric_in_sheet', 'value', 'value_display', 'matched_rows', 'numeric_rows',
             'empty_rows', 'non_numeric_rows', 'total_rows_in_sheet', 'row_excel_spans',
             'sheet_index', 'sheet_name', 'definition')
_GROUP_KEYS = ('kind', 'operation', 'operation_label', 'column', 'column_index', 'column_letter',
               'column_numeric_in_sheet', 'total_groups', 'returned_groups', 'top_n', 'order_by',
               'order_by_column', 'order_by_label', 'order_dir', 'order_dir_label', 'sorted',
               'sort_description', 'truncated_by_top_n', 'matched_rows', 'total_rows_in_sheet',
               'row_excel_spans', 'sheet_index', 'sheet_name', 'definition')
#: multi_step 的 step 白名单 = 分组结果键 + 步骤专有键。
#: 第 3 项 P2：G1 需要 ``row_excel_spans``（已在 _GROUP_KEYS 中）、``row_excel_truncated``；
#: G2 需要 ``numeric_rows``（**第 2 步输入中可数值化的个数**，由执行器用与求值同一条数值化
#: 规则算出，不在前端重算）。二者都是 **schema v1 的可选增量字段**（旧快照缺失即降级显示）。
_STEP_KEYS = _GROUP_KEYS + ('type', 'source', 'source_text', 'input_rows', 'numeric_rows',
                            'row_excel_truncated')
_ROW_KEYS = ('group_key', 'group_display', 'value', 'value_display', 'matched_rows',
             'numeric_rows', 'empty_rows', 'non_numeric_rows', 'group')
_MULTISTEP_KEYS = ('kind', 'engine', 'filename', 'sheet_index', 'sheet_name', 'definition',
                   'max_steps', 'step_count', 'value', 'value_display')


def _json_safe(value: Any) -> Any:
    """把任意结果递归转换成**标准 JSON 可序列化**的值（绝不使用 pickle / eval / repr）。"""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, 'item'):          # numpy 标量（若有）
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    return str(value)


def _history_columns(columns: Any) -> List[Dict[str, Any]]:
    """列清单 / 分组列的展示所需子集（保留 dtype / 列字母等解释性字段）。"""
    out: List[Dict[str, Any]] = []
    for col in (columns or []):
        if not isinstance(col, dict):
            continue
        out.append({k: col.get(k) for k in
                    ('name', 'index', 'excel_column', 'excel_column_letter', 'dtype',
                     'semantic_type', 'non_empty', 'null_count', 'header_row_excel')
                    if k in col})
    return out


def _history_filters(filters: Any) -> List[Dict[str, Any]]:
    """筛选条件子集（前端 formatFilters 依赖 column/resolved_column/operator/value）。"""
    out: List[Dict[str, Any]] = []
    for f in (filters or []):
        if not isinstance(f, dict):
            continue
        out.append({k: f.get(k) for k in ('column', 'resolved_column', 'operator', 'value')
                    if k in f})
    return out


def _cap_rows(rows: Any) -> tuple:
    """按上限截断行（返回 (rows, truncated)）。"""
    items = list(rows or [])
    if len(items) > EXCEL_HISTORY_MAX_ROWS:
        return items[:EXCEL_HISTORY_MAX_ROWS], True
    return items, False


def _history_group_rows(rows: Any) -> tuple:
    capped, truncated = _cap_rows(rows)
    return ([{k: r.get(k) for k in _ROW_KEYS if isinstance(r, dict) and k in r}
             for r in capped], truncated)


def _history_step_payload(step: Any) -> tuple:
    """第 1 步 / 第 2 步的展示子集（含分组列、行、排序/TOP-N、来源与口径）。"""
    if not isinstance(step, dict):
        return None, False
    rows, truncated = _history_group_rows(step.get('rows'))
    payload = {k: step.get(k) for k in _STEP_KEYS if k in step}
    payload['group_by'] = _history_columns(step.get('group_by'))
    payload['rows'] = rows
    payload['applied_filters'] = _history_filters(
        step.get('applied_filters') or step.get('filters'))
    payload['filters'] = _history_filters(step.get('filters'))
    if 'calculation' in step:
        payload['calculation'] = step.get('calculation')
    return payload, truncated


def build_excel_history_snapshot(outcome: Any) -> Optional[Dict[str, Any]]:
    """把一次 Excel 查询的**展示所需**结构化结果整理为历史快照（versioned / JSON-safe）。

    ⚠️ 白名单提取，**绝不**把整个 outcome 原样落库。明确排除：
      * new_context / new_aggregate_context / new_analysis_context（实时执行状态，历史只读展示）；
      * intent / turn / statistical_guard 等内部解析细节；
      * 任何 provider / model 密钥、prompt、原始报文。

    返回 None 表示本轮没有可展示的结构化结果（澄清 / 报错 / not_excel）→ 不写该键，
    历史消息保持纯文字摘要（旧历史即此行为）。
    """
    if not isinstance(outcome, dict):
        return None
    kind = ''
    payload: Optional[Dict[str, Any]] = None
    truncated = False

    r = outcome.get('result')
    a = outcome.get('aggregate')
    g = outcome.get('group_aggregate')
    m = outcome.get('multi_step')
    if r is not None:
        kind = 'result'
        rows, truncated = _cap_rows(r.get('rows'))
        payload = {k: r.get(k) for k in _RESULT_KEYS if k in r}
        payload['columns'] = _history_columns(r.get('columns'))
        payload['rows'] = rows
        payload['row_excel_numbers'] = list(r.get('row_excel_numbers') or [])[:len(rows)] \
            if truncated else list(r.get('row_excel_numbers') or [])
        payload['row_ranges'] = list(r.get('row_ranges') or [])[:len(rows)] \
            if truncated else list(r.get('row_ranges') or [])
        payload['applied_filters'] = _history_filters(
            r.get('applied_filters') or r.get('filters'))
        payload['filters'] = _history_filters(r.get('filters'))
        for k in ('calculation', 'calc_filter'):
            if k in r:
                payload[k] = r.get(k)
    elif a is not None:
        kind = 'aggregate'
        payload = {k: a.get(k) for k in _AGG_KEYS if k in a}
        payload['row_excel_numbers'] = list(a.get('row_excel_numbers') or [])[
            :EXCEL_HISTORY_MAX_ROWS]
        payload['applied_filters'] = _history_filters(a.get('applied_filters') or a.get('filters'))
        payload['filters'] = _history_filters(a.get('filters'))
        if 'calculation' in a:
            payload['calculation'] = a.get('calculation')
    elif g is not None:
        kind = 'group_aggregate'
        rows, truncated = _history_group_rows(g.get('rows'))
        payload = {k: g.get(k) for k in _GROUP_KEYS if k in g}
        payload['group_by'] = _history_columns(g.get('group_by'))
        payload['rows'] = rows
        payload['row_excel_numbers'] = list(g.get('row_excel_numbers') or [])[
            :EXCEL_HISTORY_MAX_ROWS]
        payload['applied_filters'] = _history_filters(g.get('applied_filters') or g.get('filters'))
        payload['filters'] = _history_filters(g.get('filters'))
        if 'calculation' in g:
            payload['calculation'] = g.get('calculation')
    elif m is not None:
        kind = 'multi_step'
        s1, t1 = _history_step_payload(m.get('step1'))
        s2, _t2 = _history_step_payload(m.get('step2'))
        payload = {k: m.get(k) for k in _MULTISTEP_KEYS if k in m}
        payload['step1'] = s1
        payload['step2'] = s2
        payload['step2_input_values'] = list(m.get('step2_input_values') or [])[
            :EXCEL_HISTORY_MAX_ROWS]
        plan = m.get('plan') if isinstance(m.get('plan'), dict) else None
        if plan is not None:
            payload['plan'] = {
                'max_steps': plan.get('max_steps'),
                'steps': [{k: s.get(k) for k in
                           ('type', 'operation', 'column', 'group_by', 'order_by', 'order_dir',
                            'top_n') if isinstance(s, dict) and k in s}
                          for s in (plan.get('steps') or [])],
            }
        truncated = bool(t1)
    if payload is None:
        return None

    doc = outcome.get('document') if isinstance(outcome.get('document'), dict) else {}
    sh = outcome.get('sheet') if isinstance(outcome.get('sheet'), dict) else {}
    snapshot = {
        'schema_version': EXCEL_HISTORY_SCHEMA_VERSION,
        'kind': kind,
        'document': {
            'document_id': doc.get('document_id') or payload.get('document_id'),
            'filename': doc.get('filename') or payload.get('filename'),
        },
        'sheet': {
            'sheet_index': sh.get('sheet_index', payload.get('sheet_index')),
            'sheet_name': sh.get('sheet_name') or payload.get('sheet_name'),
        },
        'engine': outcome.get('engine'),
        'inherited_from': outcome.get('inherited_from', ''),
        'relaxed_filters': list(outcome.get('relaxed_filters') or []),
        'continued': bool(outcome.get('continued', False)),
        'history_truncated': bool(truncated),
        'payload': payload,
    }
    return _json_safe(snapshot)


def build_excel_turn_meta(snapshot: Any) -> Dict[str, Any]:
    """Excel 轮次的 meta_info 组装（``source`` 恒为 ``'excel'``）。

    ⚠️ 契约（2026-09-28 修复双层包裹）：
      * ``snapshot`` 必须是 ``build_excel_history_snapshot(outcome)`` 的返回值**本体**
        （即 ``{schema_version, kind, document, sheet, payload, …}``）；
      * 调用方**不得**再自行包一层 ``{'excel': snapshot}`` —— 包装只在本函数做一次；
      * 传入 ``None`` -> meta 只有 ``source``（旧历史行为，前端回退纯文字）。
    """
    meta: Dict[str, Any] = {'source': SOURCE_EXCEL}
    if snapshot:
        meta['excel'] = snapshot
    return meta


def persist_excel_turn(conv_id: Optional[int], role: str, content: str, model: str,
                       snapshot: Any = None) -> None:
    """写入一条 Excel 表格查询对话记录（**生产唯一写入口**，端点与契约测试共用）。

    ``snapshot`` 语义同上：快照本体，包装由 ``build_excel_turn_meta`` 统一完成。
    任何失败只记日志、绝不冒泡（保存失败不得影响查询响应）。
    """
    if conv_id is None or not content:
        return
    try:
        conversation_service.append_message(
            conv_id, role, content, model, meta_info=build_excel_turn_meta(snapshot))
    except Exception as pe:
        logger.warning('保存表格查询对话记录失败（忽略）：%s', pe)


class ExcelNlQueryRequest(BaseModel):
    """自然语言表格查询请求体（Phase 1C + 1D）

    - session_id（Phase 1D）：会话标识，用于绑定「上一轮结构化查询上下文」。
      前端每次进入聊天页生成一个，清空对话时轮换；
      未提供时回落为 character_id（仍严格绑定 user_id）。
    """
    message: str
    character_id: Optional[str] = None   # 可选：用角色专属 model+api_key 做「意图解析」
    session_id: Optional[str] = None     # 可选：对话内连续分页的上下文归属


@router.post('/excel/nl-query')
async def excel_nl_query(
    req: ExcelNlQueryRequest,
    current_user: User = Depends(get_current_active_user)
):
    """自然语言 → Structured Query（Phase 1C / 1D / 2）

    LLM 只负责把自然语言翻译成查询参数草图（含筛选 operator），所有文件 / Sheet /
    列名都会拿真实 schema 校验；受控 SQL 由 Python 构造，实际数据行由 DuckDB
    （或 Python 参考引擎）从 representation 中精确读取。
    无法确定时返回 status='clarify'，绝不编造。
    本接口不接受任何 SQL 文本，也不接受 engine 参数（固定使用默认引擎）。
    """
    from backend.excel import nl_query as excel_nl
    from backend.services.llm_service import get_llm, LLMFactory, LLMConfig
    from backend.config.settings import settings

    message = (req.message or '').strip()
    if not message:
        raise HTTPException(status_code=400, detail='message 不能为空')

    try:
        # 可选：角色专属模型（与 /query、/speak/stream 保持一致的行为）
        llm = None
        # 后续 2A 验收：回传**服务端实际生效的模型名**（只回模型名，不含密钥），
        # 供"多模型混合稳定性"验收按样本记录 model，而不是让脚本自己猜。
        effective_model = settings.LLM_MODEL
        if req.character_id:
            from backend.services.character_service import character_service
            character = character_service.get_character(req.character_id, current_user.id)
            if character is None:
                raise HTTPException(status_code=404, detail='角色不存在')
            if character.model and character.api_key:
                llm = LLMFactory.create_llm(LLMConfig(
                    provider=settings.LLM_PROVIDER,
                    api_key=character.api_key,
                    base_url=settings.LLM_BASE_URL,
                    model=character.model,
                    embedding_model=settings.EMBEDDING_MODEL,
                    temperature=0.0,
                    max_tokens=int(os.getenv('LLM_MAX_TOKENS', '2000')),
                ))
                effective_model = character.model
        if llm is None:
            llm = get_llm()

        catalog = excel_store.list_excel_catalog(user_id=current_user.id)

        # Phase 1D / 3A：取出本会话的「上一轮查询上下文」与「上一轮统计上下文」
        # （两者分别存储、严格绑定 user_id + session_key，互不污染）
        from backend.excel.query_context import (
            get_context_store,
            get_aggregate_store,
            get_analysis_store,
            ExcelQueryContext,
            AggregateContext,
            AnalysisContext,
        )
        session_key = (req.session_id or '').strip() or (f'char:{req.character_id}' if req.character_id else 'default')
        store = get_context_store()
        agg_store = get_aggregate_store()
        analysis_store = get_analysis_store()
        prev_ctx = store.get_for_pagination(current_user.id, session_key)
        prev_agg_ctx = agg_store.get_for_inheritance(current_user.id, session_key)
        prev_analysis_ctx = analysis_store.get_for_inheritance(current_user.id, session_key)

        outcome = await excel_nl.run_nl_query(
            message,
            catalog,
            llm=llm,
            context=prev_ctx,
            aggregate_context=prev_agg_ctx,
            analysis_context=prev_analysis_ctx,
            user_id=current_user.id,
            session_key=session_key,
        )

        # 只有「成功的 Excel 结构化查询 / 统计」才成为可继续的上下文；
        # 其它情况（闲聊/澄清/失败）标记为非表格轮次，阻断错误的上下文复用。
        # 两类上下文互相隔离：统计成功会令分页上下文失效，反之亦然。
        status = outcome.get('status')
        if status == 'ok' and outcome.get('new_analysis_context'):
            # Phase 4A：多步分析成功 -> 只保留分析上下文（分页/统计上下文同时失效）
            try:
                analysis_store.save_analysis(AnalysisContext(**outcome['new_analysis_context']))
            except Exception as ana_err:
                logger.warning(f'保存多步分析上下文失败：{ana_err}')
            store.mark_other_turn(current_user.id, session_key)
            agg_store.mark_other_turn(current_user.id, session_key)
        elif status == 'ok' and outcome.get('new_aggregate_context'):
            try:
                agg_store.save_aggregate(AggregateContext(**outcome['new_aggregate_context']))
            except Exception as agg_err:
                logger.warning(f'保存统计上下文失败：{agg_err}')
            store.mark_other_turn(current_user.id, session_key)
            analysis_store.mark_other_turn(current_user.id, session_key)
        elif status == 'ok' and outcome.get('new_context'):
            try:
                store.save_excel(ExcelQueryContext(**outcome['new_context']))
            except Exception as ctx_err:  # 上下文保存失败不应影响查询结果
                logger.warning(f'保存表格查询上下文失败：{ctx_err}')
            agg_store.mark_other_turn(current_user.id, session_key)
            analysis_store.mark_other_turn(current_user.id, session_key)
        else:
            store.mark_other_turn(current_user.id, session_key)
            agg_store.mark_other_turn(current_user.id, session_key)
            analysis_store.mark_other_turn(current_user.id, session_key)

        # ===== 对话历史持久化（表格查询模式）=====
        # 背景：本端点此前完全没有落库逻辑。前端 sendMessage 中「表格查询」优先级最高且
        # 直接 return，所以「知识库 + 表格查询」同时开启时也会走这里，导致聊天记录无法保存。
        # 保存规则（避免与前端回退链路重复写入）：
        #   * status == 'not_excel' **不保存** —— 前端会回收占位并回退到 RAG / 普通聊天链路，
        #     由那条链路保存；此处若也保存会造成同一轮写两遍。
        #   * 其余状态（用户确实看到了回复）保存一轮 user + assistant。
        #   * assistant 侧保存后端已生成的结构化查询摘要（outcome['message']）。
        # 复用与 /query-with-history 相同的 conversation_service，不复制业务逻辑；
        # character_id 缺失时跳过（向后兼容）；落库失败只记日志，绝不影响查询响应。
        if status != 'not_excel':
            conv_id = _resolve_rag_conversation_id(req.character_id, current_user.id)

            def _persist_excel_turn(role: str, content: str, snapshot: Any = None) -> None:
                """端点内的薄封装：委托 ``persist_excel_turn``（生产唯一写入口）。

                ``snapshot`` = ``build_excel_history_snapshot(outcome)`` 的**本体**；
                包装成 ``meta_info['excel']`` 只由 ``build_excel_turn_meta`` 做一次
                （2026-09-28 修复：此处曾多包一层 ``{'excel': …}``，导致落库双层包裹、
                历史卡片无法恢复）。``source='excel'`` 保持不变，普通聊天链路
                仍可经 ``exclude_sources`` 整轮丢弃表格轮次。"""
                persist_excel_turn(conv_id, role, content, effective_model, snapshot)

            _persist_excel_turn('user', message)
            try:
                _history_snapshot = build_excel_history_snapshot(outcome)
            except Exception as se:            # 快照构建失败绝不影响查询响应
                _history_snapshot = None
                logger.warning('构建 Excel 历史快照失败（忽略）：%s', se)
            _persist_excel_turn('assistant', outcome.get('message') or '（表格查询完成）',
                                _history_snapshot)

        return {
            'success': True,
            'status': outcome.get('status'),
            'message': outcome.get('message'),
            # 后续 2A：provider/内部错误的稳定错误码（如 LLM_QUOTA_EXCEEDED），
            # 前端可据此做提示；**不含任何 provider 原始报文或密钥**。
            'error_code': outcome.get('error_code'),
            # 服务端实际生效的模型名（仅模型名，不含密钥/Base URL），
            # 用于"多模型混合稳定性"验收按样本记录 model。
            'llm_model': effective_model,
            'intent': outcome.get('intent'),
            'turn': outcome.get('turn'),
            'continued': outcome.get('continued', False),
            # 后续 2A：本轮是否走了「确定性分页快路径」（未调用 LLM）
            'fast_path': bool(outcome.get('fast_path')),
            'engine': outcome.get('engine'),
            'relaxed_filters': outcome.get('relaxed_filters', []),
            'inherited_from': outcome.get('inherited_from', ''),
            'aggregate': outcome.get('aggregate'),
            'group_aggregate': outcome.get('group_aggregate'),
            # Phase 4A：两步分析结果（含中间结果，便于追溯）
            'multi_step': outcome.get('multi_step'),
            'statistical_guard': bool(outcome.get('statistical_guard')),
            'analysis_guard': bool(outcome.get('analysis_guard')),
            # Phase 4B：LLM 过度规划成两步时，是否被降级为单步分组统计
            'analysis_downgraded': bool(outcome.get('analysis_downgraded')),
            # 后续 2A：单步分组统计被**确定性升级**为两步分析（未调用 LLM）
            'analysis_upgraded': bool(outcome.get('analysis_upgraded')),
            'pagination': outcome.get('pagination'),
            'document': outcome.get('document'),
            'sheet': outcome.get('sheet'),
            'query': outcome.get('query'),
            'result': outcome.get('result'),
            'candidates': outcome.get('candidates', []),
            'stage': outcome.get('stage'),
            'details': outcome.get('details'),
            'timestamp': datetime.now().isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        # 稳定性补丁：兜底也走统一分类（绝不把 provider 原始报文回给前端）
        classified = classify_llm_error(e)
        log_classified('nl_query_endpoint', classified)
        raise HTTPException(status_code=classified.http_status,
                            detail=classified.to_response())

