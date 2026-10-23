"""友链页 API 的端到端测试。

重点验证响应序列化——LinkPageOut 少了 from_attributes 时
model_validate(ORM 对象) 会抛 ValidationError，接口直接 500。
这个 bug 是线上部署后才发现的，本地测试没覆盖到。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

FRIENDS_HTML = """
<html><body>
  <div class="friend-list">
    <div class="card"><a href="https://alice.com">Alice</a></div>
    <div class="card"><a href="https://bob.com">Bob</a></div>
  </div>
</body></html>
"""


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

    from app.services.scheduler import start_scheduler

    async def _noop_start():
        return None

    monkeypatch.setattr("app.main.start_scheduler", _noop_start, raising=False)

    from app.main import app as fastapi_app

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, factory

    await engine.dispose()


@pytest_asyncio.fixture
async def fake_fetcher(monkeypatch):
    """替换掉真实的 HTTP 抓取。"""
    from app.services import link_page_service

    class FakeResponse:
        def __init__(self, text):
            self.text = text
            self.content = text.encode("utf-8")
            self.encoding = "utf-8"
            self.reason_phrase = "OK"

        def raise_for_status(self):
            return None

    class FakeClient:
        async def get(self, url):
            return FakeResponse(FRIENDS_HTML)

        async def aclose(self):
            return None

    async def _fake_fetch(url, settings=None, client=None):
        return FRIENDS_HTML

    monkeypatch.setattr(link_page_service, "fetch_link_page_html", _fake_fetch)


async def register_and_login(client):
    r = await client.post(
        "/web/register",
        data={"username": "lp_user", "password": "password123", "email": ""},
        follow_redirects=False,
    )
    assert r.status_code in (200, 302, 303, 307)

    r = await client.post(
        "/api/auth/login",
        json={"username": "lp_user", "password": "password123"},
    )
    assert r.status_code == 200


class TestLinkPageApi:
    @pytest.mark.asyncio
    async def test_add_and_list(self, client_with_app, fake_fetcher):
        client, _ = client_with_app
        await register_and_login(client)

        r = await client.post(
            "/api/link-pages",
            json={"url": "https://example.com/friends/"},
        )
        # 关键断言：响应要能正常序列化，不返回 500
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["url"] == "https://example.com/friends/"
        assert data["found_count"] == 2
        assert data["added_count"] == 2

        r = await client.get("/api/link-pages")
        assert r.status_code == 200
        assert len(r.json()) == 1

    @pytest.mark.asyncio
    async def test_friends_added_to_sites(self, client_with_app, fake_fetcher):
        """发现的好友要真的进友链列表。"""
        client, _ = client_with_app
        await register_and_login(client)

        await client.post(
            "/api/link-pages", json={"url": "https://example.com/friends/"}
        )

        r = await client.get("/api/sites")
        assert r.status_code == 200
        sites = r.json()
        assert len(sites) == 2
        assert {s["url"] for s in sites} == {"https://alice.com", "https://bob.com"}
        assert all(s["source"] == "link_page" for s in sites)

    @pytest.mark.asyncio
    async def test_duplicate_link_page_rejected(self, client_with_app, fake_fetcher):
        client, _ = client_with_app
        await register_and_login(client)

        await client.post("/api/link-pages", json={"url": "https://example.com/friends/"})
        r = await client.post("/api/link-pages", json={"url": "https://example.com/friends/"})
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_scan_again_finds_nothing_new(self, client_with_app, fake_fetcher):
        """再检测一次不应该重复添加。"""
        client, _ = client_with_app
        await register_and_login(client)

        created = await client.post(
            "/api/link-pages", json={"url": "https://example.com/friends/"}
        )
        page_id = created.json()["id"]

        r = await client.post(f"/api/link-pages/{page_id}/scan")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 2
        assert data["added"] == 0

    @pytest.mark.asyncio
    async def test_delete_link_page_keeps_sites(self, client_with_app, fake_fetcher):
        """删友链页不删已经加进来的友链。"""
        client, _ = client_with_app
        await register_and_login(client)

        created = await client.post(
            "/api/link-pages", json={"url": "https://example.com/friends/"}
        )
        page_id = created.json()["id"]

        r = await client.delete(f"/api/link-pages/{page_id}")
        assert r.status_code == 200

        r = await client.get("/api/sites")
        assert len(r.json()) == 2, "友链不该被一起删掉"

    @pytest.mark.asyncio
    async def test_toggle_active(self, client_with_app, fake_fetcher):
        client, _ = client_with_app
        await register_and_login(client)

        created = await client.post(
            "/api/link-pages", json={"url": "https://example.com/friends/"}
        )
        page_id = created.json()["id"]

        r = await client.post(f"/api/link-pages/{page_id}/toggle")
        assert r.status_code == 200
        assert r.json()["active"] is False

    @pytest.mark.asyncio
    async def test_other_user_cannot_see(self, client_with_app, fake_fetcher):
        """别人的友链页看不到。"""
        client, factory = client_with_app
        await register_and_login(client)

        await client.post("/api/link-pages", json={"url": "https://example.com/friends/"})

        # 换一个新用户
        await client.post(
            "/web/register",
            data={"username": "another", "password": "password123", "email": ""},
            follow_redirects=False,
        )
        await client.post(
            "/api/auth/login",
            json={"username": "another", "password": "password123"},
        )

        r = await client.get("/api/link-pages")
        assert r.status_code == 200
        assert r.json() == []

    @pytest.mark.asyncio
    async def test_requires_login(self, client_with_app):
        client, _ = client_with_app
        r = await client.get("/api/link-pages")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_web_page_renders(self, client_with_app, fake_fetcher):
        """后台页面能渲染。"""
        client, _ = client_with_app
        await register_and_login(client)
        await client.post("/api/link-pages", json={"url": "https://example.com/friends/"})

        r = await client.get("/web/link-pages")
        assert r.status_code == 200
        assert "友链页" in r.text
