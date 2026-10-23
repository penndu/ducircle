"""Web 后台页面测试。

重点验证：
1. 模板能真实渲染（不是返回 500）
2. 未登录访问后台页面会被拦到登录页
3. 页面里的数据不串用户
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
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

    # lifespan 里会真连 Redis 和启动调度器，测试环境跳过
    from app.services.scheduler import start_scheduler, shutdown_scheduler

    async def _noop_start():
        return None

    monkeypatch.setattr("app.main.start_scheduler", _noop_start, raising=False)

    from app.main import app as fastapi_app

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, factory

    await engine.dispose()


async def register(client, username, password="password123"):
    r = await client.post(
        "/web/register",
        data={"username": username, "password": password, "email": ""},
        follow_redirects=False,
    )
    return r


class TestAuthPages:
    @pytest.mark.asyncio
    async def test_login_page_renders(self, client_with_app):
        client, _ = client_with_app
        r = await client.get("/web/login")
        assert r.status_code == 200
        assert "登录" in r.text
        assert "用户名" in r.text

    @pytest.mark.asyncio
    async def test_register_page_renders(self, client_with_app):
        client, _ = client_with_app
        r = await client.get("/web/register")
        assert r.status_code == 200
        assert "注册" in r.text

    @pytest.mark.asyncio
    async def test_redirects_to_login_when_anonymous(self, client_with_app):
        """未登录访问后台应被拦住。"""
        client, _ = client_with_app
        for path in ["/web", "/web/sites", "/web/posts", "/web/settings"]:
            r = await client.get(path, follow_redirects=False)
            assert r.status_code == 401, f"{path} 未登录时应返回 401"
            assert "/web/login" in r.headers.get("location", "")


class TestRegistration:
    @pytest.mark.asyncio
    async def test_registration_flow(self, client_with_app):
        client, _ = client_with_app
        r = await register(client, "alice")
        assert r.status_code == 303
        assert "fcircle_session" in r.cookies or "Location" in r.headers

    @pytest.mark.asyncio
    async def test_duplicate_username_rejected(self, client_with_app):
        client, _ = client_with_app
        await register(client, "alice")
        r = await register(client, "alice")
        assert r.status_code == 400
        assert "已被占用" in r.text

    @pytest.mark.asyncio
    async def test_short_password_rejected(self, client_with_app):
        client, _ = client_with_app
        r = await register(client, "bob", password="short")
        assert r.status_code == 400
        assert "密码" in r.text

    @pytest.mark.asyncio
    async def test_invalid_username_rejected(self, client_with_app):
        client, _ = client_with_app
        r = await register(client, "a b")
        assert r.status_code == 400
        assert "用户名" in r.text


class TestDashboardPages:
    @pytest.mark.asyncio
    async def test_dashboard_renders_when_logged_in(self, client_with_app):
        client, _ = client_with_app
        await register(client, "alice")

        r = await client.get("/web")
        assert r.status_code == 200, r.text[:500]
        assert "概览" in r.text
        # 空状态的引导文案
        assert "还没有添加友链" in r.text

    @pytest.mark.asyncio
    async def test_sites_page_renders(self, client_with_app):
        client, _ = client_with_app
        await register(client, "alice")

        r = await client.get("/web/sites")
        assert r.status_code == 200
        assert "友链管理" in r.text

    @pytest.mark.asyncio
    async def test_posts_page_renders(self, client_with_app):
        client, _ = client_with_app
        await register(client, "alice")

        r = await client.get("/web/posts")
        assert r.status_code == 200
        assert "文章流" in r.text

    @pytest.mark.asyncio
    async def test_failed_page_renders(self, client_with_app):
        client, _ = client_with_app
        await register(client, "alice")

        r = await client.get("/web/failed")
        assert r.status_code == 200
        assert "抓取失败" in r.text
        assert "没有失败的站点" in r.text

    @pytest.mark.asyncio
    async def test_settings_page_renders(self, client_with_app):
        client, _ = client_with_app
        await register(client, "alice")

        r = await client.get("/web/settings")
        assert r.status_code == 200
        assert "设置" in r.text
        assert "API Key" in r.text

    @pytest.mark.asyncio
    async def test_login_error_message_displayed(self, client_with_app):
        client, _ = client_with_app
        await register(client, "alice")
        await client.post("/web/logout")

        r = await client.post(
            "/web/login",
            data={"username": "alice", "password": "wrong"},
            follow_redirects=False,
        )
        assert r.status_code == 401
        assert "用户名或密码错误" in r.text


class TestPageDataIsolation:
    @pytest.mark.asyncio
    async def test_pages_dont_leak_between_users(self, client_with_app):
        """Alice 的站点不应出现在 Bob 的页面上。"""
        client, factory = client_with_app

        await register(client, "alice")
        r = await client.post(
            "/api/sites",
            json={"name": "Alice的秘密站点", "url": "https://alice-secret.com"},
        )
        assert r.status_code == 201

        # Alice 的页面能看到自己的站点
        r = await client.get("/web/sites")
        assert "Alice的秘密站点" in r.text

        # 切换到 Bob
        await client.post("/web/logout")
        await register(client, "bob")
        r = await client.get("/web/sites")
        assert r.status_code == 200
        assert "Alice的秘密站点" not in r.text, "Bob 不应看到 Alice 的站点"

    @pytest.mark.asyncio
    async def test_dashboard_counts_isolated(self, client_with_app):
        client, factory = client_with_app

        await register(client, "alice")
        await client.post(
            "/api/sites", json={"name": "A站", "url": "https://a-site.com"}
        )
        alice_html = (await client.get("/web")).text

        await client.post("/web/logout")
        await register(client, "bob")
        bob_html = (await client.get("/web")).text

        assert "友链总数" in alice_html and "友链总数" in bob_html
        # Bob 的页面应该显示 0 个友链，不受 Alice 影响
        assert "还没有添加友链" in bob_html