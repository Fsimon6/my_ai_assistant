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

# 按 Conversation 的进程内 asyncio 锁：串行化同一对话的并发流式请求，避免历史交错/串线。
# 说明边界：该锁仅在单进程（单 uvicorn worker）内有效；多 worker 部署需改用 DB/Redis 锁。
conv_locks: dict = {}
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Depends
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import uvicorn
from backend.middleware.auth import AuthMiddleware
from backend.middleware.logging import LoggingMiddleware
from backend.utils.logger import setup_logging
from backend.config.settings import settings
from backend.utils.auth import get_current_active_user
from backend.models.user import User
from backend.utils.exceptions import global_exception_handler

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
    from backend.config import Config
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

def log_startup_configuration() -> None:
    """启动时打印生效配置（**绝不打印密钥内容**）。

    背景：`.env` 由 pydantic-settings 在**进程启动时读取一次**，运行期间使用内存配置；
    改 `.env` 后必须重启后端才会生效。这里把生效值打印出来，避免用户误以为热更新。
    """
    try:
        from backend.config.settings import settings
    except Exception as e:  # pragma: no cover - 配置不可用时不影响启动
        logger.warning('无法读取配置用于启动日志：%s', e)
        return

    def _base_host(url: str) -> str:
        try:
            from urllib.parse import urlparse
            return urlparse(url or '').netloc or '(未设置)'
        except Exception:
            return '(解析失败)'

    key_state = 'true' if (settings.API_KEY or '').strip() else 'false'
    logger.info(
        'configuration loaded at startup（改 .env 需重启进程才生效）\n'
        '  environment        = %s\n'
        '  llm.provider       = %s\n'
        '  llm.model          = %s\n'
        '  llm.base_host      = %s\n'
        '  llm.temperature    = %s\n'
        '  embedding.model    = %s\n'
        '  embedding.provider = %s\n'
        '  api_key_configured = %s\n'
        '  cors_origins       = %s\n'
        '  vector_db_path     = %s',
        settings.ENVIRONMENT,
        settings.LLM_PROVIDER,
        settings.LLM_MODEL,
        _base_host(settings.LLM_BASE_URL),
        os.getenv('LLM_TEMPERATURE', '0.7（默认）'),
        settings.EMBEDDING_MODEL,
        settings.EMBEDDING_PROVIDER,
        key_state,
        ','.join(settings.BACKEND_CORS_ORIGINS or []) if isinstance(
            settings.BACKEND_CORS_ORIGINS, (list, tuple)) else settings.BACKEND_CORS_ORIGINS,
        settings.VECTOR_DB_PATH,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时
    logger.info(' AI助手启动中...')
    # 配置校验失败必须冒泡，禁止静默吞掉（JWT_SECRET 等必填项缺失即启动失败）
    Config.validate_config()
    # 生效配置可见性（不打印任何密钥内容）
    log_startup_configuration()

    # 创建必要目录
    os.makedirs('./data/chroma_db', exist_ok=True)
    os.makedirs('./data/uploads', exist_ok=True)

    # 启动时创建数据库表（init_db 内部会导入所有模型，确保 Base.metadata 已注册）
    from backend.database.base import init_db
    try:
        init_db()
    except Exception as e:
        logger.error(f' 数据库表初始化失败：{e}')
        raise

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

# 注册全局异常处理器（仅兜底未显式处理的异常 → 统一信封，不影响 401/403/404/422）
app.add_exception_handler(Exception, global_exception_handler)

# 认证 / 日志中间件必须在应用启动前（构建期）注册，禁止在请求处理中重复 add_middleware
app.add_middleware(
    AuthMiddleware,
    public_paths=[
        "/docs", "/redoc", "/openapi.json", "/health",
        "/api/v1/auth/login", "/api/v1/auth/register"
    ]
)
app.add_middleware(LoggingMiddleware)


# 设置CORS（跨域资源共享）——必须注册在最外层（最后 add_middleware），
# 否则鉴权中间件返回的 401/403 等错误响应会缺少 Access-Control-Allow-Origin 头，
# 浏览器会把它当成 CORS 错误而非正常 401，导致前端无法触发“登录过期→跳登录”逻辑。
cors_origins = settings.BACKEND_CORS_ORIGINS
if isinstance(cors_origins, str):
    cors_origins = [o.strip() for o in cors_origins.split(',') if o.strip()]
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(origin) for origin in cors_origins],
        allow_credentials=True,
        allow_methods=['*'],
        allow_headers=['*'],
    )
else:
    # 未配置时的默认 CORS（仅在显式无配置时生效）
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

    return response

# Pydantic模型
class SpeakRequest(BaseModel):
    message: str
    stream: Optional[bool] = False

# ======== Characters API 路由 ========
@app.post("/api/v1/characters/{character_id}/speak/stream")
async def speak_to_character_stream(
        character_id: str,
        request: SpeakRequest,
        current_user: User = Depends(get_current_active_user),
):
    """流式对话接口（真实 LLM：DashScope，复用已验证的 llm_service）

    第二十阶段：对话历史持久化
    - 用户消息在流式开始前写入 conversations/texts
    - 加载该 (用户, 角色) 的当前对话历史注入 LLM（多轮上下文）
    - AI 回复在完整流式结束后写入一次（不按 chunk 写库）
    """

    # 延迟导入，避免循环依赖（与 main.py 中其它 service 调用风格一致）
    from backend.services.llm_service import get_llm, LLMFactory, LLMConfig
    from backend.services.character_service import character_service
    from backend.services.conversation_service import (
        conversation_service, SOURCE_CHAT, SOURCE_EXCEL,
    )

    # 归属查询（防 IDOR）：角色不存在 / 不属于当前用户（含已删除）→ 404
    character = character_service.get_character(character_id, user_id=current_user.id)
    if character is None:
        raise HTTPException(status_code=404, detail='角色不存在或已被删除')

    async def generate():
        full_response = ''
        lock = None
        effective_model = settings.LLM_MODEL  # 默认全局模型；保证异常分支始终有定义，避免 UnboundLocalError
        try:
            # 角色设定（已在接口层做归属查询与 404，此处直接使用）
            system_prompt = character.system_prompt
            character_name = character.name
            character_model = character.model
            character_api_key = character.api_key

            # 解析并定位当前对话（与加锁在同一作用域内，保证并发串行化）
            conv_id = None
            cid_int = None
            try:
                cid_int = int(character_id)
            except (TypeError, ValueError):
                cid_int = None

            # 角色级 model + api_key 覆盖：两者都填写才使用角色专属配置，否则使用全局默认。
            # 必须在用户消息落库前确定 effective_model，避免 UnboundLocalError。
            if character_model and character_api_key:
                effective_model = character_model
                llm = LLMFactory.create_llm(LLMConfig(
                    provider=settings.LLM_PROVIDER,
                    api_key=character_api_key,
                    base_url=settings.LLM_BASE_URL,
                    model=character_model,
                    embedding_model=settings.EMBEDDING_MODEL,
                    temperature=float(os.getenv('LLM_TEMPERATURE', '0.7')),
                    max_tokens=int(os.getenv('LLM_MAX_TOKENS', '2000')),
                ))
            else:
                effective_model = settings.LLM_MODEL
                llm = get_llm()
            logger.info(f'普通 Chat 使用模型：{effective_model}（角色级={bool(character_model and character_api_key)}）')

            if character is not None and cid_int is not None:
                conv_id = conversation_service.get_or_create_conversation_id(
                    current_user.id, cid_int
                )
                # 同一对话的并发流式串行化：避免 user/assistant 历史交错或上下文串线
                lock = conv_locks.setdefault(conv_id, asyncio.Lock())
                await lock.acquire()
                # 先落库用户消息（来源标记 = 普通聊天）
                conversation_service.append_message(
                    conv_id, 'user', request.message, effective_model,
                    meta_info={'source': SOURCE_CHAT},
                )
                # 加载历史（已含刚写入的用户消息），注入 LLM 多轮上下文。
                # 来源隔离：Excel 表格查询属独立工具能力，其整轮（user + assistant）
                # 一律不进入普通聊天上下文，避免历史表格结果污染其他聊天模式。
                history = conversation_service.get_history(
                    conv_id, exclude_sources=[SOURCE_EXCEL],
                )
            else:
                history = []

            messages = [{"role": "system", "content": system_prompt}] + history

            async for chunk in llm.chat_completion(messages=messages, stream=True):
                full_response += chunk
                yield json.dumps({
                    "type": "chunk",
                    "content": chunk,
                    "timestamp": datetime.now().isoformat(),
                }) + "\n"

            # AI 回复在完整流式结束后写入一次（不按 chunk 写库）
            if conv_id is not None and full_response:
                conversation_service.append_message(
                    conv_id, 'assistant', full_response, effective_model,
                    meta_info={'source': SOURCE_CHAT},
                )

            yield json.dumps({
                "type": "complete",
                "content": full_response,
                "character_name": character_name,
                "timestamp": datetime.now().isoformat(),
            }) + "\n"
        except Exception as e:
            logger.error(f"流式对话LLM调用失败：{e}")
            # Streaming 中断/异常时补一条明确的失败助手消息，保持 user/assistant 历史对称
            if conv_id is not None:
                try:
                    conversation_service.append_message(
                        conv_id, 'assistant', '（内容生成失败，请重试）', effective_model,
                        meta_info={'source': SOURCE_CHAT},
                    )
                except Exception:
                    pass
            yield json.dumps({
                "type": "error",
                "error": str(e),
                "timestamp": datetime.now().isoformat(),
            }) + "\n"
        finally:
            if lock is not None:
                try:
                    lock.release()
                except Exception:
                    pass

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
# 导入API路由（统一使用 backend 包结构，避免运行目录/扁平导入问题）
from backend.api.v1 import api_router
from backend.api.v1.rag import router as rag_router

# api_router 内部子路由（auth/characters）均以各自模块前缀挂到 /api/v1 下
app.include_router(api_router, prefix=Config.API_V1_PREFIX)
# rag_router 自身已带 /api/v1/rag 前缀，直接挂载避免重复前缀
app.include_router(rag_router)
print('API router loaded')


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


