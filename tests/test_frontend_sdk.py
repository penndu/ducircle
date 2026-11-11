"""前台调用相关的测试。

两件事：
1. CORS 中间件——只对 /v1/* 开放，且按域名白名单
2. 前台调用代码文件存在且契约正确（字段名、参数名要和接口对得上）
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest_asyncio.fixture
async def client(monkeypatch):
    import app.db as db_module
    from app.models import Base

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "get_session_factory", lambda: factory)

    async def _check_db():
        return True

    monkeypatch.setattr(db_module, "check_db", _check_db)

    from app.core.ratelimit import RateLimiter

    async def _allow(self, user_id, cost=1.0, rate=None, burst=None):
        return True

    monkeypatch.setattr(RateLimiter, "allow", _allow)

    from app.main import app as fastapi_app

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await engine.dispose()


class TestCorsMiddleware:
    @pytest.mark.asyncio
    async def test_preflight_allowed_for_whitelisted_origin(self):
        """白名单内的来源，预检要放行。"""
        from app.api.cors_middleware import CorsMiddleware

        async def _app(scope, receive, send):
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            })
            await send({"type": "http.response.body", "body": b"ok"})

        mw = CorsMiddleware(_app, allowed_origins=["https://allowed.example"])

        sent: list = []

        async def _send(message):
            sent.append(message)

        async def _receive():
            return {"type": "http.request", "body": b""}

        scope = {
            "type": "http",
            "method": "OPTIONS",
            "path": "/v1/all",
            "headers": [
                (b"origin", b"https://allowed.example"),
                (b"access-control-request-method", b"GET"),
            ],
        }
        await mw(scope, _receive, _send)

        start = sent[0]
        assert start["status"] == 204
        headers = dict(start["headers"])
        assert headers[b"access-control-allow-origin"] == b"https://allowed.example"
        assert b"X-API-Key" in headers[b"access-control-allow-headers"]

    @pytest.mark.asyncio
    async def test_preflight_rejected_for_other_origin(self):
        from app.api.cors_middleware import CorsMiddleware

        async def _app(scope, receive, send):
            pass

        mw = CorsMiddleware(_app, allowed_origins=["https://allowed.example"])

        sent: list = []

        async def _send(message):
            sent.append(message)

        async def _receive():
            return {"type": "http.request", "body": b""}

        scope = {
            "type": "http",
            "method": "OPTIONS",
            "path": "/v1/all",
            "headers": [(b"origin", b"https://evil.example")],
        }
        await mw(scope, _receive, _send)

        assert sent[0]["status"] == 403
        assert b"access-control-allow-origin" not in dict(sent[0]["headers"])

    @pytest.mark.asyncio
    async def test_empty_whitelist_blocks_everything(self):
        """没配白名单就一个都不放行。"""
        from app.api.cors_middleware import CorsMiddleware

        async def _app(scope, receive, send):
            pass

        mw = CorsMiddleware(_app, allowed_origins=[])

        sent: list = []

        async def _send(message):
            sent.append(message)

        async def _receive():
            return {"type": "http.request", "body": b""}

        scope = {
            "type": "http",
            "method": "OPTIONS",
            "path": "/v1/all",
            "headers": [(b"origin", b"https://x.com")],
        }
        await mw(scope, _receive, _send)
        assert sent[0]["status"] == 403

    @pytest.mark.asyncio
    async def test_non_v1_path_skips_cors(self):
        """/api/* 不走 CORS 分支，直接进业务。"""
        from app.api.cors_middleware import CorsMiddleware

        reached = []

        async def _app(scope, receive, send):
            reached.append(scope["path"])
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            })
            await send({"type": "http.response.body", "body": b"ok"})

        mw = CorsMiddleware(_app, allowed_origins=["https://x.com"])

        sent: list = []

        async def _send(message):
            sent.append(message)

        async def _receive():
            return {"type": "http.request", "body": b""}

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/posts",
            "headers": [(b"origin", b"https://x.com")],
        }
        await mw(scope, _receive, _send)

        assert reached == ["/api/posts"], "应该直接进业务逻辑"
        headers = dict(sent[0]["headers"])
        assert b"access-control-allow-origin" not in headers

    @pytest.mark.asyncio
    async def test_non_v1_paths_have_no_cors(self, client):
        """后台接口不能开放跨域——那些靠 Cookie 会话鉴权。"""
        r = await client.get(
            "/api/posts", headers={"Origin": "https://allowed.example"}
        )
        assert "access-control-allow-origin" not in r.headers

    @pytest.mark.asyncio
    async def test_web_paths_have_no_cors(self, client):
        r = await client.get(
            "/web", headers={"Origin": "https://allowed.example"}
        )
        assert "access-control-allow-origin" not in r.headers

    def test_default_origins_is_empty(self):
        """默认不开放跨域——要开必须显式配置。"""
        from app.core.config import get_settings

        assert get_settings().cors_origins == ""

    def test_middleware_normalizes_origins(self):
        from app.api.cors_middleware import CorsMiddleware

        async def _noop(scope, receive, send):
            pass

        mw = CorsMiddleware(_noop, allowed_origins=["https://x.com/", "  ", ""])
        assert mw.allowed == {"https://x.com"}


class TestFrontendSdkFiles:
    def test_all_sdk_files_exist(self):
        from pathlib import Path

        for name in [
            "ducircle.js",
            "ducircle-react.jsx",
            "ducircle-vue.js",
            "ducircle-hexo.md",
        ]:
            path = Path("web/static") / name
            assert path.exists(), f"缺少 {name}"
            assert path.stat().st_size > 500, f"{name} 内容过短"

    def test_js_uses_correct_api_endpoints(self):
        """调用的路径要和真实接口一致。"""
        from pathlib import Path

        content = Path("web/static/ducircle.js").read_text(encoding="utf-8")
        assert "/v1/all" in content
        assert "/v1/friends" in content
        assert "/v1/stats" in content

    def test_js_uses_correct_auth_header(self):
        from pathlib import Path

        content = Path("web/static/ducircle.js").read_text(encoding="utf-8")
        assert "X-API-Key" in content

    def test_js_uses_correct_query_params(self):
        """start/end 是条数偏移，参数名不能错。"""
        from pathlib import Path

        content = Path("web/static/ducircle.js").read_text(encoding="utf-8")
        for param in ["start", "end", "rule", "site_id", "only_summarized"]:
            assert param in content, f"缺少参数 {param}"

    def test_js_uses_correct_response_fields(self):
        """字段名要和 V1Post 对得上。"""
        from pathlib import Path

        content = Path("web/static/ducircle.js").read_text(encoding="utf-8")
        for field in ["title", "link", "author", "avatar", "created", "summary", "score"]:
            assert f".{field}" in content or f"p.{field}" in content, (
                f"缺少字段 {field}"
            )

    def test_js_escapes_html(self):
        """标题里的尖括号必须转义，否则能注入 HTML。"""
        from pathlib import Path

        content = Path("web/static/ducircle.js").read_text(encoding="utf-8")
        assert "function esc(" in content
        assert "textContent" in content

    def test_react_component_contract(self):
        from pathlib import Path

        content = Path("web/static/ducircle-react.jsx").read_text(encoding="utf-8")
        assert "export default" in content
        assert "X-API-Key" in content
        assert "/v1/all" in content
        assert "apiKey" in content

    def test_vue_component_contract(self):
        from pathlib import Path

        content = Path("web/static/ducircle-vue.js").read_text(encoding="utf-8")
        assert "export default" in content
        assert "X-API-Key" in content
        assert "/v1/all" in content
        assert "apiKey" in content

    def test_hexo_template_contract(self):
        from pathlib import Path

        content = Path("web/static/ducircle-hexo.md").read_text(encoding="utf-8")
        assert "X-API-Key" in content
        assert "/v1/all" in content
        # Hexo 模板要用 EJS/Liquid 语法设变量
        assert "{% set" in content


class TestDocsPage:
    @pytest.mark.asyncio
    async def test_docs_page_has_frontend_code_section(self, client):
        await client.post(
            "/web/register",
            data={"username": "alice", "password": "password123", "email": ""},
            follow_redirects=False,
        )
        html = (await client.get("/docs")).text
        assert "前台调用代码" in html
        assert "ducircle.js" in html
        assert "CORS_ORIGINS" in html
        # 四种语言版本都要有
        assert "原生 JS" in html
        assert "React" in html
        assert "Vue 3" in html
