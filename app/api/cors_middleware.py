"""只读 API 的跨域支持。

后台页面和带密钥的接口（/api/*、/web/*）**不开放**跨域——
那些接口靠 Cookie 会话鉴权，开放跨域等于把会话暴露给任意站点。

只有 /v1/* 开放：它是给博客前端调用的只读接口，
用 X-API-Key 头鉴权，Key 掌握在博主自己手里。

为什么不直接用 allow_origins=["*"]：
请求要带 X-API-Key 这个自定义头，浏览器会先发 OPTIONS 预检，
而 "*" 不允许携带自定义头，必须逐个列出来源。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("ducircle.cors")


class CorsMiddleware:
    """按域名白名单放行 /v1/* 的跨域请求。

    纯 ASGI 实现，和限流中间件一个层级——
    FastAPI 自带的 CORSMiddleware 是全局生效的，
    没法只对某几个路径开放。
    """

    def __init__(self, app, allowed_origins: list[str] | None = None) -> None:
        self.app = app
        self.allowed = {
            o.strip().rstrip("/") for o in (allowed_origins or []) if o.strip()
        }
        if self.allowed:
            logger.info("跨域白名单：%s", ", ".join(sorted(self.allowed)))

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # 只对只读 API 开放跨域
        if not path.startswith("/v1/"):
            await self.app(scope, receive, send)
            return

        origin = self._origin_of(scope)
        allowed = origin in self.allowed if origin else False

        if scope.get("method") == "OPTIONS":
            # 预检请求：直接回 CORS 头，不进业务逻辑
            headers = []
            if allowed:
                headers = [
                    (b"access-control-allow-origin", origin.encode()),
                    (b"access-control-allow-methods", b"GET, OPTIONS"),
                    (b"access-control-allow-headers", b"X-API-Key, Content-Type"),
                    (b"access-control-max-age", b"86400"),
                    (b"vary", b"Origin"),
                ]
            await self._send(send, 204 if allowed else 403, headers)
            return

        if not allowed:
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"access-control-allow-origin", origin.encode()),
                        (b"access-control-expose-headers", b"X-Total"),
                        (b"vary", b"Origin"),
                    ]
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)

    @staticmethod
    def _origin_of(scope) -> str | None:
        for key, value in scope.get("headers", []):
            if key == b"origin":
                return value.decode("latin-1").rstrip("/")
        return None

    @staticmethod
    async def _send(send, status: int, headers: list) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": b""})
