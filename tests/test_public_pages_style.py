"""公开页面统一视觉的回归测试。

落地页 /docs /health 三个公开入口共用一套样式、导航和页脚；
/health 还要保证监控用的 JSON 契约不被 HTML 版本破坏。
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

BRAND_CSS = "--brand:#1f6b54"
FOOTER_BADGE = "杜老师说旗下服务"
FOOTER_HOME = 'href="https://dusays.com/"'


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

    from app.core.ratelimit import RateLimiter, RedisManager

    async def _ping(self):
        return True

    async def _allow(self, user_id, cost=1.0, rate=None, burst=None):
        return True

    monkeypatch.setattr(RedisManager, "ping", _ping)
    monkeypatch.setattr(RateLimiter, "allow", _allow)

    from app.main import app as fastapi_app

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await engine.dispose()


class TestSharedChrome:
    @pytest.mark.asyncio
    async def test_all_public_pages_share_brand_and_footer(self, client):
        """首页、文档、状态页都得是同一套皮，页脚徽标与主站链接不能漏。"""
        for path, headers in [
            ("/", {}),
            ("/docs", {}),
            ("/health", {"Accept": "text/html"}),
        ]:
            r = await client.get(path, headers=headers)
            assert r.status_code == 200, f"{path}: {r.text[:300]}"
            html = r.text
            assert BRAND_CSS in html, f"{path} 缺少公共样式"
            assert FOOTER_BADGE in html, f"{path} 缺少旗下服务徽标"
            assert FOOTER_HOME in html, f"{path} 缺少杜老师说主站链接"
            assert 'href="https://dusays.com/posts/530/"' in html
            assert "cdn.tailwindcss.com" not in html


class TestApiDocsPage:
    @pytest.mark.asyncio
    async def test_docs_page_is_branded_not_swagger(self, client):
        r = await client.get("/docs")
        assert r.status_code == 200, r.text[:400]
        html = r.text
        for text in [
            "API 文档", "打开交互式文档", "/docs/swagger", "/openapi.json",
            "鉴权与约定", "开放接口清单", "X-API-Key", "/v1/all",
            "复制这一行就能跑",
        ]:
            assert text in html, f"文档页缺少「{text}」"
        # 新页面是自家模板，不是 FastAPI 默认的 Swagger 壳
        assert "swagger-ui-bundle" not in html
        assert "SERVICE STATUS" not in html

    @pytest.mark.asyncio
    async def test_endpoint_list_is_generated_from_openapi(self, client):
        from app.main import _v1_endpoints

        rows = _v1_endpoints()
        paths = {row["path"] for row in rows}
        assert {"/v1/all", "/v1/post", "/v1/friends", "/v1/stats"} <= paths
        assert all(row["method"] == "GET" for row in rows)
        all_row = next(row for row in rows if row["path"] == "/v1/all")
        assert "start" in all_row["params"]
        assert "end" in all_row["params"]

    @pytest.mark.asyncio
    async def test_swagger_and_openapi_still_work(self, client):
        swagger = await client.get("/docs/swagger")
        assert swagger.status_code == 200
        assert "swagger-ui" in swagger.text.lower()
        schema = await client.get("/openapi.json")
        assert schema.status_code == 200
        assert "/v1/all" in schema.json()["paths"]
        # 页面路由不进 schema，文档清单保持稳定
        assert "/docs" not in schema.json()["paths"]


class TestHealthPage:
    @pytest.mark.asyncio
    async def test_json_contract_unchanged(self, client):
        """curl / HEALTHCHECK / 监控默认拿到 JSON，字段一个不少。"""
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        data = r.json()
        assert {"status", "db", "version", "build", "built_at"} <= set(data)
        assert data["status"] == "ok"
        assert data["db"] == "ok"

    @pytest.mark.asyncio
    async def test_browser_gets_branded_status_page(self, client):
        r = await client.get(
            "/health",
            headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        html = r.text
        for text in [
            "服务状态", "所有服务运行正常", "API 服务", "数据库", "Redis",
            "定时任务", "构建版本", "原始 JSON 响应", 'http-equiv="refresh"',
        ]:
            assert text in html, f"状态页缺少「{text}」"

    @pytest.mark.asyncio
    async def test_format_override(self, client):
        forced_json = await client.get(
            "/health?format=json", headers={"Accept": "text/html"}
        )
        assert forced_json.headers["content-type"].startswith("application/json")
        assert forced_json.json()["status"] == "ok"

        forced_html = await client.get("/health?format=html")
        assert forced_html.headers["content-type"].startswith("text/html")
        assert "服务状态" in forced_html.text

    @pytest.mark.asyncio
    async def test_status_page_survives_redis_failure(self, client, monkeypatch):
        """Redis 挂了状态页也要能打开，并且如实显示降级。"""
        from app.core.ratelimit import RedisManager

        async def _boom(self):
            raise RuntimeError("redis down")

        monkeypatch.setattr(RedisManager, "ping", _boom)
        r = await client.get("/health", headers={"Accept": "text/html"})
        assert r.status_code == 200, r.text[:400]
        assert "不可用 · 已降级" in r.text
        assert "所有服务运行正常" in r.text  # db 仍正常，整体不算 down
