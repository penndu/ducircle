"""限流中间件。

应用层限流用 Redis 令牌桶（app/core/ratelimit.py），与 Nginx 层的
limit_req 形成双保险：Nginx 防网络洪水，应用层防单用户刷接口。

认证类的接口（登录、注册）单独用更严格的桶——否则可以被用来
暴力猜密码或批量注册刷号。
"""

from __future__ import annotations

from fastapi import Request, status
from fastapi.responses import JSONResponse

from app.core.ratelimit import RateLimiter

# 认证接口的独立限流参数：更慢、突发更小
AUTH_RATE = 1
AUTH_BURST = 5

# 只读 API 的参数
API_RATE = 5
API_BURST = 15

# 后台操作：按用户 ID 限流，未登录时退化为按 IP
WEB_RATE = 3
WEB_BURST = 10


class RateLimitMiddleware:
    """纯 ASGI 中间件，在路由匹配前执行，避免解析 body。"""

    def __init__(self, app) -> None:
        self.app = app
        self.limiter = RateLimiter()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")

        bucket = self._pick_bucket(path)
        if bucket is None:
            await self.app(scope, receive, send)
            return

        rate, burst = bucket

        # 用户 ID 优先（已登录），否则用 IP
        user_id = scope.get("state", {}).get("user_id") if scope.get("state") else None
        if user_id is None:
            client = scope.get("client")
            user_id = f"ip:{client[0]}" if client else "ip:unknown"

        allowed = await self.limiter.allow(
            str(user_id), rate=rate, burst=burst
        )
        if not allowed:
            response = JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "请求过于频繁，请稍后再试"},
                headers={"Retry-After": "60"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    @staticmethod
    def _pick_bucket(path: str):
        if path.startswith("/api/auth/login") or path.startswith("/api/auth/register"):
            return AUTH_RATE, AUTH_BURST
        if path.startswith("/v1/"):
            return API_RATE, API_BURST
        if path.startswith("/api/"):
            return WEB_RATE, WEB_BURST
        return None  # 静态资源、健康检查不限流


class TenantContextMiddleware:
    """在路由处理前清空租户上下文。

    每个请求开始时必须重置——否则同一工作线程上处理的上一个请求的
    user_id 会泄漏到下一个请求，构成越权。测试里的 conftest 做的
    就是这件事，这里是生产环境的等价保障。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from app.core.tenant import _tenant_bypass, _tenant_id

        _tenant_id.set(None)
        _tenant_bypass.set(False)
        try:
            await self.app(scope, receive, send)
        finally:
            _tenant_id.set(None)
            _tenant_bypass.set(False)