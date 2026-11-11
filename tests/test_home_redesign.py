"""首页改版回归测试。

覆盖两件事：
1. 公开落地页能渲染，且数据库抖动时不会把首页带崩；
2. 后台概览新增的趋势 / 最新文章查询在真实 SQLite 会话里跑得通，
   并且不会串用户。
"""

from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest_asyncio.fixture
async def client_with_app(monkeypatch):
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
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, factory

    await engine.dispose()


async def _register(client, username):
    return await client.post(
        "/web/register",
        data={"username": username, "password": "password123", "email": ""},
        follow_redirects=False,
    )


class TestLandingPage:
    @pytest.mark.asyncio
    async def test_landing_renders(self, client_with_app):
        client, _ = client_with_app
        r = await client.get("/")
        assert r.status_code == 200, r.text[:400]
        assert "把友链" in r.text
        assert "能做什么" in r.text
        assert "/v1/posts" in r.text
        assert "免费开始" in r.text
        # 落地页自带样式，不该再依赖 Tailwind CDN
        assert "cdn.tailwindcss.com" not in r.text
        assert "cdn.jsdelivr.net/npm/tailwindcss" not in r.text

    @pytest.mark.asyncio
    async def test_landing_survives_stats_failure(self, client_with_app, monkeypatch):
        """统计查询挂了也要能看到首页，只是隐藏数字条。"""
        from contextlib import asynccontextmanager

        import app.db as db_module

        @asynccontextmanager
        async def _broken():
            raise RuntimeError("db down")
            yield  # pragma: no cover

        monkeypatch.setattr(db_module, "session_scope", _broken)
        client, _ = client_with_app
        r = await client.get("/")
        assert r.status_code == 200, r.text[:400]
        assert "把友链" in r.text
        assert "已接入友链站点" not in r.text

    @pytest.mark.asyncio
    async def test_public_stats_degrade_to_none(self, monkeypatch):
        """数据库不可用时 _public_overview 返回空值，不抛异常。"""
        from contextlib import asynccontextmanager

        import app.db as db_module
        import app.main as main_module

        @asynccontextmanager
        async def _broken():
            raise RuntimeError("db down")
            yield  # pragma: no cover

        monkeypatch.setattr(db_module, "session_scope", _broken)
        result = await main_module._public_overview()
        assert result == {"sites": None, "posts": None}


class TestDashboardRedesign:
    @pytest.mark.asyncio
    async def test_dashboard_sections_render(self, client_with_app):
        client, _ = client_with_app
        await _register(client, "alice")

        r = await client.get("/web")
        assert r.status_code == 200, r.text[:500]
        for text in [
            "概览", "友链总数", "近 7 天新增", "近 14 天收录趋势",
            "最新文章", "抓取健康", "API Key", "还没有添加友链",
        ]:
            assert text in r.text, f"概览页缺少「{text}」"

    @pytest.mark.asyncio
    async def test_trend_and_recent_posts_use_real_data(self, client_with_app):
        client, factory = client_with_app
        await _register(client, "alice")
        r = await client.post(
            "/api/sites",
            json={"name": "木子学习笔记", "url": "https://muzi.example.com"},
        )
        assert r.status_code == 201

        from app.models import Post, Site, User

        async with factory() as db:
            user = (await db.execute(select(User))).scalar_one()
            site = (await db.execute(select(Site))).scalar_one()
            db.add(
                Post(
                    user_id=user.id,
                    site_id=site.id,
                    title="用 RSS 搭一套自己的信息流",
                    link="https://muzi.example.com/posts/1/",
                    guid_hash="a" * 40,
                    created=datetime.now(),
                    crawled_at=datetime.now(),
                    summary="从抓取到去重的一整套记录",
                    ai_score=8.6,
                )
            )
            await db.commit()

        html = (await client.get("/web")).text
        assert "用 RSS 搭一套自己的信息流" in html
        assert "木子学习笔记" in html
        assert "从抓取到去重的一整套记录" in html
        # 趋势图里至少有一根非空的柱子
        assert 'title="' in html

    @pytest.mark.asyncio
    async def test_recent_posts_are_tenant_isolated(self, client_with_app):
        client, factory = client_with_app
        await _register(client, "alice")
        await client.post(
            "/api/sites",
            json={"name": "Alice 的站点", "url": "https://alice.example.com"},
        )

        from app.models import Post, Site, User

        async with factory() as db:
            user = (await db.execute(select(User))).scalar_one()
            site = (await db.execute(select(Site))).scalar_one()
            db.add(
                Post(
                    user_id=user.id,
                    site_id=site.id,
                    title="Alice 的私密文章",
                    link="https://alice.example.com/secret/",
                    guid_hash="b" * 40,
                    crawled_at=datetime.now(),
                )
            )
            await db.commit()

        assert "Alice 的私密文章" in (await client.get("/web")).text

        await client.post("/web/logout")
        await _register(client, "bob")
        bob_html = (await client.get("/web")).text
        assert "Alice 的私密文章" not in bob_html
