"""只读 API 的跨域支持。

后台页面和带密钥的接口（/api/*、/web/*）**不开放**跨域——
那些接口靠 Cookie 会话鉴权，开放跨域等于把会话暴露给任意站点。

只有 /v1/* 开放：它是给博客前端调用的只读接口，
用 X-API-Key 头鉴权，Key 掌握在博主自己手里。
**不需要 Cookie**，所以放开来源不会带来 CSRF 风险。

支持两种模式（``CORS_ORIGINS`` 配置）：

- ``*``（默认）—— 不限制调用网站。任何站点都能在浏览器里直接调 /v1/*。
  这是「API 支持跨站使用」的默认行为：博主把博客部署在哪个域名，
  都不用来后台改配置。
- 逗号分隔的域名白名单 —— 只允许这几个来源。
  适合想收紧范围（比如只允许自己的博客域名）的部署。

为什么不用 FastAPI 自带的 CORSMiddleware：
它是全局生效的，没法只对 /v1/* 开放；而 /api/* 一旦带上
``Access-Control-Allow-Origin``，就等于把 Cookie 会话暴露给任意站点。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("ducircle.cors")

# 允许所有来源的哨兵值
ANY_ORIGIN = "*"


class CorsMiddleware:
    """按域名白名单放行 /v1/* 的跨域请求。

    纯 ASGI 实现，和限流中间件一个层级。
    """

    def __init__(self, app, allowed_origins: list[str] | None = None) -> None:
        self.app = app
        origins = [o.strip() for o in (allowed_origins or []) if o.strip()]

        # 只要配置里出现 "*"，就放开全部来源。
        # 用回显 Origin 而不是字面量 "*"：两者语义相同（都不允许带凭据），
        # 但回显能保证 Vary: Origin 一定生效，反向代理/浏览器缓存
        # 不会把 A 站点的响应错给 B 站点。
        self.allow_any = ANY_ORIGIN in origins
        self.allowed = {
            o.rstrip("/") for o in origins if o != ANY_ORIGIN
        }

        if self.allow_any:
            logger.info("跨域：允许任意来源访问 /v1/*（不限制调用网站）")
        elif self.allowed:
            logger.info("跨域白名单：%s", ", ".join(sorted(self.allowed)))
        else:
            logger.info("跨域：未配置来源，浏览器将无法跨站调用 /v1/*")

    def _is_allowed(self, origin: str | None) -> bool:
        if self.allow_any:
            # 预检必须带 Origin；没带说明不是浏览器跨域请求，不注入头
            return bool(origin)
        if not origin:
            return False
        return origin in self.allowed

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
        allowed = self._is_allowed(origin)

        if scope.get("method") == "OPTIONS":
            # 预检请求：直接回 CORS 头，不进业务逻辑
            headers = []
            if allowed:
                headers = [
                    (b"access-control-allow-origin", (origin or "").encode()),
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
                        (b"access-control-allow-origin", (origin or "").encode()),
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
