from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse
from typing import List, Optional
import json
import logging
from datetime import datetime

from services.rag_service import get_rag_service
from services.document_service import save_uploaded_file


router = APIRouter(prefix='/api/v1/rag', tags=['RAG'])
logger = logging.getLogger(__name__)


@router.post('/upload')
async def upload_document(
    file: UploadFile = File(...),
    metadata: Optional[str] = None
):
    """上传并处理文档"""
    try:
        rag_service = get_rag_service()

        # 验证文件类型
        allowed_types = ['.pdf', '.txt', '.docx', '.md']
        file_ext = '.' + file.filename.split('.')[-1] if '.' in file.filename else ''

        if file_ext.lower() not in allowed_types:
            raise HTTPException(
                status_code=400,
                detail=f'不支持的文件类型。支持的类型：{",".join(allowed_types)}'
            )

        # 保存文件
        file_path = save_uploaded_file(file)

        # 解析元数据
        parsed_metadata = {}
        if metadata:
            try:
                parsed_metadata = json.loads(metadata)
            except:
                parsed_metadata = {'custom_metadata': metadata}

        # 处理文档
        result = await rag_service.process_and_store_document(
            file_path,
            parsed_metadata
        )

        if result['success']:
            return {
                'success': '文档处理成功',
                'filename': result['filename'],
                'total_chunks': result['total_chunks'],
            }
        else:
            raise HTTPException(
                status_code=500,
                detail=f'文档处理失败：{result.get("error")}'
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'上传文档失败：{e}')
        raise HTTPException(
            status_code=500,
            detail=f'上传文档失败：{str(e)}'
        )


@router.get('/query')
async def query_document(
    query: str,
    stream: bool = False,
    context_count: int = 3,
):
    """查询文档"""
    try:
        rag_service = get_rag_service()

        if stream:
            async def generate():
                async for chunk in rag_service.rag_query(
                    query=query,
                    context_count=context_count,
                    stream=True
                ):
                    yield json.dumps({
                        'type': 'chunk',
                        'content': chunk,
                        'timestamp': datetime.now().isoformat()
                    }) + '\n'

                yield json.dumps({
                    'type': 'chunk',
                    'content': '',
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
                query=query,
                context_count=context_count,
                stream=False
            ):
                response_text += chunk

            return {
                'response': response_text,
                'query': query,
                'timestamp': datetime.now().isoformat()
            }

    except Exception as e:
        logger.error(f'查询失败：{e}')
        raise HTTPException(
            status_code=500,
            detail=f'查询失败：{str(e)}'
        )


@router.get('/query-with-history')
async def query_with_history(
        query: str,
        history: List[dict],
        stream: bool = False,
        context_count: int = 3,
):
    """带历史记录的查询"""
    try:
        rag_service = get_rag_service()

        if stream:
            async def generate():
                async for chunk in rag_service.query_with_history(
                    query=query,
                    history=history,
                    context_count=context_count,
                ):
                    yield json.dumps({
                        'type': 'chunk',
                        'content': '',
                        'timestamp': datetime.now().isoformat()
                    }) + '\n'

                yield json.dumps({
                    'type': 'complete',
                    'content': '',
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
            async for chunk in rag_service.query_with_history(
                query=query,
                history=history,
                context_count=context_count,
            ):
                response_text += chunk

            return {
                'response': response_text,
                'query': query,
                'timestamp': datetime.now().isoformat()
            }

    except Exception as e:
        logger.error(f'带历史查询失败：{e}')
        raise HTTPException(
            status_code=500,
            detail=f'查询失败：{str(e)}'
        )


@router.get('/collection-info')
async def get_collection_info():
    """获取向量数据库信息"""
    try:
        from services.vector_service import get_vector_store_manager
        vector_store = get_vector_store_manager()
        info = vector_store.get_collection_info()

        return {
            'collection_info': info,
            'timestamp': datetime.now().isoformat()
        }

    except Exception as e:
        logger.error(f'获取集合信息失败：{e}')
        raise HTTPException(
            status_code=500,
            detail=f'获取集合信息失败：{str(e)}'
        )


@router.delete('/documents')
async def delete_documents(document_ids: List[str]):
    """删除文档"""
    try:
        from services.vector_service import get_vector_store_manager
        success = await vector_store.delete_documents(document_ids)

        if success:
            return {
                'message': '文档删除成功',
                'delete_ids': document_ids,
                'timestamp': datetime.now().isoformat()
            }
        else:
            raise HTTPException(
                status_code=500,
                detail='文档删除失败'
            )

    except Exception as e:
        logger.error(f'删除文档失败：{e}')
        raise HTTPException(
            status_code=500,
            detail=f'删除文档失败“：{str(e)}'
        )

