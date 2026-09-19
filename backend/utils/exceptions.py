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
        # 稳定性补丁：原实现写成 `super.__init__(...)`（漏了调用运算符），
        # 任何子类一旦实例化都会抛出与业务无关的 TypeError。
        super().__init__(status_code=status_code, detail=detail)
        self.error_code = error_code or f'ERR_{status_code}'


class ValidationException(AppException):
    """验证异常"""
    def __init__(self, detail: str = '请求参数验证失败'):
        super().__init__(status_code=400, detail=detail, error_code='ERR_VALIDATION')


class AuthenticationException(AppException):
    """认证异常"""
    def __init__(self, detail: str = '认证失败'):
        super().__init__(status_code=401, detail=detail, error_code='ERR_AUTH')


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
    """全局异常处理器（统一错误信封，非 DEBUG 不泄露 traceback/SQL）

    注意：FastAPI 对 HTTPException / RequestValidationError 已有默认处理器，
    本处理器仅兜底“未显式处理的异常”（通常映射为 500），不影响 401/403/404/422。
    """
    # 记录错误日志
    logger.error(f'异常请求：{request.method} {request.url}')
    logger.error(f'异常类型：{type(exc).__name__}')
    logger.error(f'异常信息：{str(exc)}')
    logger.error(f'请求客户端：{request.client}')

    import os
    is_debug = os.getenv('DEBUG', 'False').lower() == 'true'

    if isinstance(exc, AppException):
        status_code = exc.status_code
        response_data = {
            'success': False,
            'error': {
                'code': exc.error_code,
                'message': exc.detail,
                'type': type(exc).__name__,
            },
            'timestamp': datetime.now().isoformat(),
        }
    elif isinstance(exc, RequestValidationError):
        errors = [{
            'field': ''.join(str(loc) for loc in e.get('loc', [])),
            'message': e.get('msg'),
            'type': e.get('type'),
        } for e in exc.errors()]
        status_code = 422
        response_data = {
            'success': False,
            'error': {
                'code': 'ERR_VALIDATION',
                'message': '请求参数验证失败',
                'detail': errors,
                'type': 'RequestValidationError',
            },
            'timestamp': datetime.now().isoformat(),
        }
    elif isinstance(exc, HTTPException):
        status_code = exc.status_code
        response_data = {
            'success': False,
            'error': {
                'code': f'ERR_{exc.status_code}',
                'message': exc.detail,
                'type': 'HTTPException',
            },
            'timestamp': datetime.now().isoformat(),
        }
    else:
        status_code = 500
        response_data = {
            'success': False,
            'error': {
                'code': 'ERR_INTERNAL',
                'message': str(exc) if is_debug else '服务器内部错误，请稍后重试',
                'type': type(exc).__name__,
            },
            'timestamp': datetime.now().isoformat(),
        }

    if is_debug and not isinstance(exc, (AppException, RequestValidationError, HTTPException)):
        response_data['error']['traceback'] = traceback.format_exc()
        response_data['error']['detail'] = str(exc)

    logger.error('未处理异常：', exc_info=True)
    return JSONResponse(status_code=status_code, content=response_data)

from datetime import datetime
