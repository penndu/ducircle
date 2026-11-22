"""管理员总览：跨租户统计的正确性、权限、页面渲染。

最容易出错的两点：
1. 忘了豁免租户过滤 → 全站数字变成管理员一个人的数字（看着正常，其实全错）。
2. 兑换码/AI 用量统计口径错 → 把用户自带 Key 的调用算进平台成本。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

PASSWORD = "password123"


@pytest_asyncio.fixture
async def app_env(monkeypatch):
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

    yield fastapi_app, factory
    await engine.dispose()


@pytest_asyncio.fixture
async def make_client(app_env):
    app, _ = app_env
    created: list[AsyncClient] = []

    def _make() -> AsyncClient:
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        created.append(client)
        return client

    yield _make
    for client in created:
        await client.aclose()


async def _register_login(client, username):
    r = await client.post(
        "/api/auth/register", json={"username": username, "password": PASSWORD}
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert r.status_code == 200, r.text


class TestAdminOverview:
    @pytest.mark.asyncio
    async def test_requires_admin(self, make_client):
        anon = make_client()
        assert (await anon.get("/api/admin/overview")).status_code == 401

        await _register_login(make_client(), "alice")  # 首个账号自动管理员
        bob = make_client()
        await _register_login(bob, "bob")
        assert (await bob.get("/api/admin/overview")).status_code == 403

    @pytest.mark.asyncio
    async def test_overview_counts_across_tenants(self, app_env, make_client):
        """管理员自己的数据之外，其他用户的数据也必须算进去。"""
        _, factory = app_env
        from app.models import AIQuotaLog, Post, RedeemCode, Site, User

        admin = make_client()
        await _register_login(admin, "alice")
        bob = make_client()
        await _register_login(bob, "bob")

        async with factory() as db:
            users = (await db.execute(__import__("sqlalchemy").select(User))).scalars().all()
            admin_user = next(u for u in users if u.username == "alice")
            bob_user = next(u for u in users if u.username == "bob")
            from sqlalchemy import select as sa_select

            # 给 bob 加 2 个站点 + 2 篇文章，管理员自己 1 个
            for owner, n in ((bob_user, 2), (admin_user, 1)):
                for i in range(n):
                    site = Site(
                        user_id=owner.id,
                        name=f"{owner.username}-site-{i}",
                        url=f"https://{owner.username}-{i}.example.com",
                        url_hash=f"h-{owner.username}-{i}",
                    )
                    db.add(site)
                    await db.flush()
                    db.add(
                        Post(
                            user_id=owner.id,
                            site_id=site.id,
                            title=f"post-{i}",
                            link=f"https://{owner.username}-{i}.example.com/p",
                            guid_hash=f"g-{owner.username}-{i}",
                            summary="摘要" if i == 0 else None,
                        )
                    )
            # 只给 bob 记一条摘要用量
            db.add(
                AIQuotaLog(
                    user_id=bob_user.id,
                    post_id=1,
                    quota_date=date.today(),
                    provider="test",
                    model="test-model",
                )
            )
            # 兑换码：1 张已用
            db.add(
                RedeemCode(
                    code="FC2-TEST-0001",
                    batch_id=None,
                    plan="trial",
                    credits=100,
                    price_cents=200,
                    used_by=bob_user.id,
                    used_by_name=bob_user.username,
                    used_at=datetime.now(),
                )
            )
            await db.commit()

        data = (await admin.get("/api/admin/overview")).json()
        assert data["users"]["total"] == 2
        assert data["sites"]["total"] == 3, "必须跨租户统计，不能只算管理员的"
        assert data["posts"]["total"] == 3
        assert data["posts"]["summarized"] == 2, "两个用户各有一篇 i==0 的摘要"
        assert data["ai"]["total"] == 1
        assert data["ai"]["from_credit"] == 0, "普通用量不该算成兑换次数"
        assert data["ai"]["free"] == 1
        assert data["redeem"]["codes_total"] == 1
        assert data["redeem"]["used"] == 1
        assert data["redeem"]["credits_issued"] == 100

    @pytest.mark.asyncio
    async def test_admin_page_renders(self, make_client):
        admin = make_client()
        await _register_login(admin, "alice")
        r = await admin.get("/web/admin/overview")
        assert r.status_code == 200, r.text[:600]
        for text in (
            "用户总数", "友链总数", "文章总数", "摘要生成",
            "AI 摘要用量", "兑换码", "抓取健康", "近 14 天收录趋势",
        ):
            assert text in r.text, f"总览页缺少「{text}」"
        assert "settings-grid" in r.text

    @pytest.mark.asyncio
    async def test_admin_home_defaults_to_overview(self, make_client):
        admin = make_client()
        await _register_login(admin, "alice")
        r = await admin.get("/web/admin", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/web/admin/overview"

    @pytest.mark.asyncio
    async def test_overview_tab_is_first_in_nav(self, make_client):
        admin = make_client()
        await _register_login(admin, "alice")
        html = (await admin.get("/web/admin/overview")).text
        users_pos = html.find('href="/web/admin/users"')
        overview_pos = html.find('href="/web/admin/overview"')
        assert overview_pos != -1 and users_pos != -1
        assert overview_pos < users_pos, "总览 Tab 必须在用户管理左侧"
