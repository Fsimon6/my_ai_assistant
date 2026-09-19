import time
import logging
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

# 注意：不能使用 "uvicorn.access" logger —— 它被 uvicorn 的 AccessFormatter 接管，
# 其 formatMessage 会尝试把消息 unpack 成 5 个字段，导致
# "ValueError: not enough values to unpack (expected 5, got 0)"。
# 改用普通 logger，由标准 Formatter 输出，避免破坏请求链。
logger = logging.getLogger("app.request")


class LoggingMiddleware(BaseHTTPMiddleware):
    """日志中间件：记录请求处理时间和状态"""

    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        response = await call_next(request)
        process_time = (time.time() - start_time) * 1000    # ms

        # 记录日志
        logger.info(
            f'{request.method} {request.url.path} '
            f'{response.status_code} {process_time:.2f}ms'
        )
        return response