# -*- coding: utf-8 -*-
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
import logging
import traceback

logger = logging.getLogger(__name__)


class AppException(HTTPException):
    """应用基础异常"""

    def __init__(self, status_code: int, detail: str, error_code: str = None):
        super.__init__(status_code=status_code, detail=detail)
        self.error_code = error_code or f'ERR_{status_code}'


class ValidationException(AppException):
    """验证异常"""
    def __init__(self, detail: str = '请求参数验证失败'):
        super.__init__(status_code=400, detail=detail)
        error_code = f'ERR_VALIDATION'


class AuthenticationException(AppException):
    """认证异常"""
    def __init__(self, detail: str = '认证失败'):
        super.__init__(status_code=401, detail=detail, error_code='ERR_AUTH')


class AuthorizationException(AppException):
    """授权异常"""
    def __init__(self, detail: str = '权限不足'):
        super().__init__(status_code=403, detail=detail, error_code='ERR_AUTHZ')


class NotFoundException(AppException):
    """资源不存在异常"""
    def __init__(self, detail: str = '资源不存在'):
        super().__init__(status_code=404, detail=detail, error_code='ERR_NOT_FOUND')


class RateLimitException(AppException):
    """频率限制异常"""
    def __init__(self, detail: str = '请求过于频繁'):
        super().__init__(status_code=429, detail=detail, error_code='ERR_RATE_LIMIT')


class ExternalServiceException(AppException):
    """外部服务异常"""
    def __init__(self, detail: str = '外部服务异常'):
        super().__init__(status_code=502, detail=detail, error_code='ERR_EXTERNAL')


class LLMServiceException(ExternalServiceException):
    """大模型服务异常"""
    def __init__(self, detail: str = '大模型服务异常'):
        super().__init__(detail=detail)
        self.error_code = 'ERR_LLM'


class VectorDBException(AppException):
    """向量数据库异常"""
    def __init__(self, detail: str = '向量数据库异常'):
        super().__init__(status_code=500, detail=detail, error_code='ERR_VECTOR_DB')


# 异常处理器
async def global_exception_handler(request: Request, exc: Exception):
    """全局异常处理器"""

    # 记录错误日志
    logger.error(f'异常请求：{request.method} {request.url}')
    logger.error(f'异常类型：{type(exc).__name__}')
    logger.error(f'异常信息：{str(exc)}')
    logger.error(f'请求客户端：{request.client}')

    # 生产环境隐藏详细错误
    import os
    is_debug = os.getenv('DEBUG', 'False').lower() == 'true'

    if isinstance(exc, AppException):
        # 应用自定义异常
        response_data = {
            'success': False,
            'error': {
                'code': exc.error_code,
                'message': exc.detail,
                'type': type(exc).__name__
            },
            'timestamp': __import__('datetime').datetime.now().isoformat()
        }

        if is_debug:
            response_data['error']['traceback'] = traceback.format_exc()
            return JSONResponse(
                status_code=exc.status_code,
                content=response_data,
            )
        elif isinstance(exc, RequestValidationError):
            # 验证错误
            errors = []
            for error in exc.errors():
                errors.append({
                    'field': ''.join(str(loc) for loc in error.get('loc', [])),
                    'message': error.get('msg'),
                    'type': error.get('type')
                })

            response_data = {
                'success': False,
                'error': {
                    'code': 'ERR_VALIDATION',
                    'message': '请求参数验证失败',
                    'detail': errors,
                    'type': 'RequestValidationError'
                },
                'timestamp': __import__('datetime').datetime.now().isoformat()
            }

            return JSONResponse(
                status_code=422,
                content=response_data,
            )

        elif isinstance(exc, HTTPException):
            # FastAPI HTTP异常
            response_data = {
                'success': False,
                'error': {
                    'code': f'ERR_{exc.status_code}',
                    'message': exc.detail,
                    'type': 'HTTPException'
                },
                'timestamp': __import__('datetime').datetime.now().isoformat()
        }

        if is_debug:
            response_data['error']['traceback'] = traceback.format_exc()
            return JSONResponse(
                status_code=exc.status_code,
                content=response_data,
            )

    else:
        # 其他未处理异常
        response_data = {
            'success': False,
            'error': {
                'code': 'ERR_INTERNAL',
                'message': '服务器内部错误' if not is_debug else str(exc),
                'type': type(exc).__name__
            },
            'timestamp': __import__('datetime').datetime.now().isoformat()
        }

        if is_debug:
            response_data['error']['traceback'] = traceback.format_exc()
            response_data['error']['detail'] = str(exc)

        logger.error('未处理异常：', exc_info=True)

        return JSONResponse(
            status_code=500,
            content=response_data,
        )

from datetime import datetime
