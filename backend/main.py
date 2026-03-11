# -*- coding: utf-8 -*-
"""
FastAPI主应用入口
"""
import sys
import os
import time
import json
import asyncio
import logging
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import uvicorn
from backend.middleware.auth import AuthMiddleware
from backend.middleware.logging import LoggingMiddleware
from backend.utils.logger import setup_logging

# 添加项目根目录到Python路径
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)     # backend的父目录

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

# 导入配置
try:
    from .config import Config
    print('√ 配置加载成功')
except Exception as e:
    print(f'× 配置加载失败：{e}')
    # 创建默认配置
    class Config:
        PROJECT_NAME = '我的AI知识库助手'
        APP_VERSION = '1.0.0'
        APP_DESCRIPTION = '基于大模型的本地知识库智能问答助手'
        HOST = '0.0.0.0'
        PORT = 8000
        DEBUG = True
        BACKEND_CORS_ORIGINS = ['http://localhost:5173', 'http://127.0.0.1:5173']
        API_V1_PREFIX = '/api/v1'
        APP_NAME = 'My AI Assistant'
        DATABASE_URL = 'sqlite:///./data/ai_assistant.db'
        WINDOWS_WEMP_DIR = os.getenv('TEMP', '/tmp')

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时
    logger.info(' AI助手启动中...')
    try:
        Config.validate_config()
    except:
        pass

    # 创建必要目录
    os.makedirs('./data/chroma_db', exist_ok=True)
    os.makedirs('./data/uploads', exist_ok=True)

    logger.info(' 初始化完成')

    yield

    # 关闭时
    logger.info(' AI助手后端关闭')

# 初始化日志
setup_logging()
app = FastAPI(
    title=Config.APP_NAME,
    version=Config.APP_VERSION,
    description=Config.APP_DESCRIPTION,
    lifespan=lifespan,
    docs_url='/docs',
    redoc_url='/redoc',
    contact={
        'name': ':范西蒙',
        'email': '2376709678@qq.com',
    },
    license_info={
        'name': 'MIT',
        'url': 'https://opensource.org/licenses/MIT',
    }
)

# 设置CORS（跨域资源共享）
if hasattr(Config, 'BACKEND_CORS_ORIGINS') and Config.BACKEND_CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(origin) for origin in Config.BACKEND_CORS_ORIGINS],
        allow_credentials=True,
        allow_methods=['*'],
        allow_headers=['*'],
    )
else:
    # 使用默认CORS配置
    app.add_middleware(
        CORSMiddleware,
        allow_origins=['http://localhost:5173', 'http://127.0.0.1:5173'],
        allow_credentials=True,
        allow_methods=['*'],
        allow_headers=['*'],
    )


# 添加请求日志中间件
@app.middleware('http')
async def log_request(request: Request, call_next):
    """请求日志中间件"""
    start_time = time.time()

    # 记录请求信息
    logger.info(f'{request.method} {request.url.path}')
    response = await call_next(request)

    # 计算处理时间
    process_time = time.time() - start_time
    response.headers['X-Process-Time'] = str(process_time)

    # 记录响应信息
    logger.info(f'    {response.status_code} ({process_time:.3f}s)')

    app.add_middleware(LoggingMiddleware)
    app.add_middleware(AuthMiddleware, public_paths=[
        "/docs", "/redoc", "/openai.json", "/health", "/api/v1/auth/login", "/api/v1/auth/register"
    ])

    return response

# Pydantic模型
class SpeakRequest(BaseModel):
    message: str
    stream: Optional[bool] = False

class QueryRequest(BaseModel):
    query: str
    stream: Optional[bool] = False
    context_count: Optional[int] = 3

class QueryWithHistoryRequest(BaseModel):
    query: str
    history: List[dict]
    stream: Optional[bool] = False
    context_count: Optional[int] = 3

# ======== RAG API 路由 ========
@app.post('/api/v1/rag/upload')
async def upload_document(
    file: UploadFile = File(...),
    metadata: Optional[str] = None
):
    """上传并处理文件"""
    try:
        # 这里导入以避免循环依赖
        from services.rag_service import get_rag_service
        from services.document_service import save_uploaded_file

        rag_service = get_rag_service()

        # 验证文件类型
        allowed_types = ['.pdf', '.docx', '.txt', '.md']
        file_ext = '.' + file.filename.split('.')[-1] if '.' in file.filename else ''

        if file_ext not in allowed_types:
            raise HTTPException(
                status_code=400,
                detail=f'不支持的文件类型，支持的类型：{",".join(allowed_types)}'
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
            parsed_metadata,
        )

        if result['success']:
            return {
                'success': True,
                'message': '文档处理成功',
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
        logger.error(f'上传文件失败：{e}')
        raise HTTPException(
            status_code=500,
            detail=f'上传文件失败：{str(e)}'
        )

@app.post('/api/v1/rag/query')
async def query_document(
    request: QueryRequest,
):
    """查询文档"""
    try:
        from services.rag_service import get_rag_service
        rag_service = get_rag_service()

        if request.stream:
            async def generate():
                async for chunk in rag_service.rag_query(
                    query=request.query,
                    context_count=request.context_count,
                    stream=True
                ):
                    yield json.dumps({
                        'type': 'chunk',
                        'content': chunk,
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
                    'X-Accel-Buffering': 'no',
                }
            )
        else:
            response_text = ''
            async for chunk in rag_service.rag_query(
                query=request.query,
                context_count=request.context_count,
                stream=False
            ):
                response_text += chunk

            return {
                'success': True,
                'response': response_text,
                'query': request.query,
                'timestamp': datetime.now().isoformat()
            }

    except Exception as e:
        logger.error(f'查询失败：{e}')
        raise HTTPException(
            status_code=500,
            detail=f'查询失败：{str(e)}'
        )

@app.post('/api/v1/rag/query-with-history')
async def query_with_history(
    request: QueryWithHistoryRequest,
):
    """带历史记录的查询"""
    try:
        from services.rag_service import get_rag_service
        rag_service = get_rag_service()

        if request.stream:
            async def generate():
                async for chunk in rag_service.query_with_history(
                    query=request.query,
                    history=request.history,
                    context_count=request.context_count,
                ):
                    yield json.dumps({
                        'type': 'chunk',
                        'content': chunk,
                        'timestamp': datetime.now().isoformat()
                    }) + '\n'

                yield json.dumps({
                    'type': 'complete',
                    'complete': '',
                    'timestamp': datetime.now().isoformat()
                }) + '\n'

            return StreamingResponse(
                generate(),
                media_type='application/x-ndjson',
                headers={
                    'Cache-Control': 'no-cache',
                    'X-Accel-Buffering': 'no',
                }
            )
        else:
            response_text = ''
            async for chunk in rag_service.query_with_history(
                query=request.query,
                history=request.history,
                context_count=request.context_count
            ):
                response_text += chunk

            return {
                'success': True,
                'response': response_text,
                'query': request.query,
                'timestamp': datetime.now().isoformat()
            }

    except Exception as e:
        logger.error(f'带历史查询失败：{e}')
        raise HTTPException(
            status_code=500,
            detail=f'查询失败：{str(e)}'
        )

@app.get('/api/v1/rag/collection-info')
async def get_collection_info():
    """获取向量数据库信息"""
    try:
        from services.vector_service import get_vector_store_manager
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

# ======== Characters API 路由 ========
@app.post("/api/v1/characters/{character_id}/speak/stream")
async def speak_to_character_stream(character_id: str, request: SpeakRequest):
    """流式对话接口"""

    async def generate():
        # 模拟流式响应
        response_text = f'这是角色{character_id}的流式回复：{request.message}'
        words = response_text.split()

        for word in words:
            # 发送每个单词
            yield json.dumps({
                'type': 'chunk',
                'content': word + ' ',
                'timestamp': datetime.now().isoformat()
            }) + '\n'
            await asyncio.sleep(0.1)    # 模拟AI思考时间

        # 发送完成信号
        yield json.dumps({
            'type': 'complete',
            'content': '',
            'total_length': len(response_text),
            'timestamp': datetime.now().isoformat()
        }) + '\n'

    return StreamingResponse(
        generate(),
        media_type='application/x-ndjson',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'
        }
    )

@app.post("/api/v1/characters/{character_id}/speak")
async def speak_to_character(request: SpeakRequest, character_id: str):
    """普通对话接口（非流式）"""
    return {
        'success': True,
        'response': f'这是角色{character_id}的回复：{request.message}',
        'character_id': character_id,
        'timestamp': datetime.now().isoformat()
    }

# ======== 基础API路由 ========
# 导入API路由
# try:
#     from api.v1 import api_router
#
#     app.include_router(api_router, prefix=Config.API_V1_PREFIX)
#     print('✅️ API路由加载成功')
# except ImportError as e:
#     print(f'⚠️   API路由加载失败：{e}')


# 根路径
@app.get('/')
async def root():
    """API根路径，返回基本信息"""
    system_info = {
        'platform': sys.platform,
        'python_version': sys.version,
        'host': Config.HOST,
        'port': Config.PORT,
        'vector_db_path': './data/chroma_db'
    }

    return {
        'success': True,
        'data': {
            'app': Config.APP_NAME,
            'versions': Config.APP_VERSION,
            'description': Config.APP_DESCRIPTION,
            'system': system_info,
            'docs': '/docs',
            'redoc': '/redoc',
            'database': 'SQLite',
            'vector_db': 'ChromaDB',
            'rag_system': '已集成'
        },
        'message': '欢迎使用My AI Assistant API'
    }

# 健康检查端点
@app.get('/health')
async def health_check():
    """健康检查端点"""
    return {
        'status': 'healthy',
        'platform': sys.platform,
        'service': Config.APP_NAME,
        'versions': Config.APP_VERSION,
        'timestamp': datetime.now().isoformat(),
        'rag_system': 'active'
    }

# Windows信息端点
@app.get('/windows-info')
async def windows_info():
    """Windows系统信息（仅Windows可用）"""
    if sys.platform == 'win32':
        raise HTTPException(status_code=400, detail='此端点仅适用于Windows系统')

    import platform
    return {
        'system': platform.system(),
        'release': platform.release(),
        'versions': platform.version(),
        'machine': platform.machine(),
        'processor': platform.processor(),
    }

# 在 main.py 中添加测试路由（在合适的位置）
@app.get("/test-characters")
async def test_characters():
    return {"message": "测试路由正常工作"}

@app.get("/api/v1/test-characters")
async def test_v1_characters():
    return {"message": "API v1 测试路由正常工作"}

@app.get("/api/v1/test-rag")
async def test_rag():
    return {'message': 'RAG系统API测试路由正常工作'}


if __name__ == '__main__':
    print(f"\n🚀 启动我的AI知识库助手服务...")
    print(f"📌 后端地址: http://{Config.HOST}:{Config.PORT}")
    print(f"📚 API文档: http://{Config.HOST}:{Config.PORT}/docs")
    print(f'   RAG系统： 已集成')
    print(f'   向量数据库： 。/data/chroma_db')
    print("🛑 按 Ctrl+C 停止服务\n")

    uvicorn.run(
        app,
        host=Config.HOST,
        port=Config.PORT,
        reload=getattr(Config, 'DEBUG', True),
    )


