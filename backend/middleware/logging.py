import time
import logging
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger("uvicorn.access")


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