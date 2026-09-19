"""
统一API响应格式
"""
from typing import Any, Dict, Optional, Union
from fastapi import status
from fastapi.responses import JSONResponse
import datetime

class ApiResponse:
    """API响应工具类"""

    @staticmethod
    def success(
        data: Any = None,
        message: str = '请求成功',
        code: int = 200,
        **kwargs
    ) -> JSONResponse:
        """成功响应"""
        response_data = {
            'success': True,
            'message': message,
            'code': code,
            'data': data,
            'timestamp': datetime.datetime.now().isoformat(),
            **kwargs
        }
        return JSONResponse(
            content=response_data,
            status_code=status.HTTP_200_OK
        )

    @staticmethod
    def error(
            message: str = '请求失败',
            code: int = 400,
            error: Optional[Dict] = None,
            **kwargs
    ) -> JSONResponse:
        """错误响应"""
        response_data = {
            'success': False,
            'message': message,
            'code': code,
            'error': error or {},
            'timestamp': datetime.datetime.now().isoformat(),
            **kwargs
        }

        # 根据错误码设置HTTP状态码
        status_code_mapping = {
            400: status.HTTP_400_BAD_REQUEST,
            401: status.HTTP_401_UNAUTHORIZED,
            403: status.HTTP_403_FORBIDDEN,
            404: status.HTTP_404_NOT_FOUND,
            422: status.HTTP_422_UNPROCESSABLE_ENTITY,
            500: status.HTTP_500_INTERNAL_SERVER_ERROR
        }

        status_code = status_code_mapping.get(code, status.HTTP_400_BAD_REQUEST)

        return JSONResponse(
            content=response_data,
            status_code=status_code
        )

    @staticmethod
    def created(data: Any = None, message: str = '创建成功') -> JSONResponse:
        """创建成功响应"""
        return ApiResponse.success(
            data=data,
            message=message,
            code=201
        )

    @staticmethod
    def not_found(message: str = '资源不存在') -> JSONResponse:
        """未找到响应"""
        return ApiResponse.error(
            message=message,
            code=404
        )

    @staticmethod
    def unauthorized(message: str = '未授权访问') -> JSONResponse:
        """未授权响应"""
        return ApiResponse.error(
            message=message,
            code=401
        )

# 创建全局实例
api_response = ApiResponse()