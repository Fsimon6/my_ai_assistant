# backend/middleware/auth.py
import logging
from typing import Optional
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from backend.utils.security import decode_access_token
from backend.models.user import User

logger = logging.getLogger(__name__)


class AuthMiddleware(BaseHTTPMiddleware):
    """认证中间件：解析 JWT Token，将当前用户附加到 request.state"""

    def __init__(self, app: ASGIApp, public_paths: Optional[list] = None):
        super().__init__(app)
        self.public_paths = public_paths or [
            "/docs",
            "/redoc",
            "/openapi.json",
            "/health",
            "/api/v1/auth/login",
            "/api/v1/auth/register",
        ]

    async def dispatch(self, request: Request, call_next):
        # CORS 预检（OPTIONS）由浏览器发起、不携带 token，必须由内层 CORSMiddleware
        # 正常应答（200 + CORS 头）。此处若拦截会直接 401，导致预检失败、真实请求被浏览器拦截。
        if request.method == "OPTIONS":
            return await call_next(request)

        # 跳过公开路径
        if any(request.url.path.startswith(path) for path in self.public_paths):
            return await call_next(request)

        # BaseHTTPMiddleware 在 dispatch 中直接 raise HTTPException 会被 Starlette
        # 包装成 500（anyio EndOfStream），因此统一在此捕获并以 JSONResponse 返回 401。
        try:
            auth_header = request.headers.get("Authorization")
            if not auth_header or not auth_header.startswith("Bearer "):
                raise HTTPException(status_code=401, detail="Missing or invalid token")

            token = auth_header.split(" ")[1]
            try:
                payload = decode_access_token(token)
                user_id = int(payload.get("sub"))
                if user_id is None:
                    raise HTTPException(status_code=401, detail="Invalid token")
            except Exception as e:
                logger.warning(f"Token decode error: {e}")
                raise HTTPException(status_code=401, detail="Invalid token")

            # 从数据库获取用户（可选）
            from backend.database.base import SessionLocal
            db = SessionLocal()
            try:
                user = db.query(User).filter(User.id == int(user_id)).first()
                if not user:
                    raise HTTPException(status_code=401, detail="User not found")
                request.state.user = user
                request.state.user_id = user.id
            finally:
                db.close()
        except HTTPException as e:
            return JSONResponse(
                status_code=e.status_code,
                content={"detail": e.detail},
                headers=getattr(e, "headers", None) or None,
            )

        response = await call_next(request)
        return response
