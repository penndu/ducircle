"""多用户平台：管理员用户管理。

覆盖三类必须守住的东西：

1. **首个账号自动成为管理员** —— 否则全新部署没人能进管理页。
2. **普通用户不能碰管理接口** —— 403，而不是「页面能打开但里面是空的」。
3. **管理员不能把自己锁在门外** —— 停用/降权/删除自己都要被拒绝。

另外验证管理员看到的是**全站**数据，不是自己被租户过滤后的那点数据。
这是最容易被漏掉的一条：认证依赖会把管理员 ID 写进租户上下文，
跨用户查询不显式豁免的话，管理页会把「全站只有管理员自己」当作事实。
"""

from __future__ import annotations

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
    """按需创建客户端。

    每个客户端有独立的 cookie jar——「管理员」和「普通用户」必须
    用各自的客户端，否则 Set-Cookie 会互相覆盖，测出来的权限是错的。
    """
    app, _ = app_env
    created: list[AsyncClient] = []

    def _make() -> AsyncClient:
        client = AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        )
        created.append(client)
        return client

    yield _make
    for client in created:
        await client.aclose()


async def register(client: AsyncClient, username: str) -> dict:
    r = await client.post(
        "/api/auth/register", json={"username": username, "password": PASSWORD}
    )
    assert r.status_code == 201, r.text
    return r.json()


async def login(client: AsyncClient, username: str, password: str = PASSWORD) -> dict:
    r = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200, r.text
    return r.json()


async def make_admin_and_user(make_client):
    """alice = 管理员（首个注册），bob = 普通用户。"""
    admin = make_client()
    await register(admin, "alice")
    await login(admin, "alice")

    bob = make_client()
    await register(bob, "bob")
    await login(bob, "bob")
    return admin, bob


class TestBootstrapAdmin:
    @pytest.mark.asyncio
    async def test_first_user_becomes_admin(self, make_client):
        client = make_client()
        data = await register(client, "first")
        assert data["is_admin"] is True, "首个注册的账号必须自动成为管理员"

    @pytest.mark.asyncio
    async def test_second_user_is_not_admin(self, make_client):
        first = make_client()
        await register(first, "first")

        second = make_client()
        data = await register(second, "second")
        assert data["is_admin"] is False


class TestAdminApiAccess:
    @pytest.mark.asyncio
    async def test_admin_can_list_users(self, make_client):
        admin, _ = await make_admin_and_user(make_client)

        r = await admin.get("/api/admin/users")
        assert r.status_code == 200, r.text
        body = r.json()
        names = {u["username"] for u in body["items"]}
        assert names == {"alice", "bob"}, "管理员必须能看到所有账号"
        assert body["total"] == 2

    @pytest.mark.asyncio
    async def test_normal_user_gets_403(self, make_client):
        _, bob = await make_admin_and_user(make_client)

        for path in [
            "/api/admin/users",
            "/api/admin/users/1",
            "/api/admin/users/overview",
        ]:
            r = await bob.get(path)
            assert r.status_code == 403, f"{path} 普通用户不该能访问"

    @pytest.mark.asyncio
    async def test_anonymous_gets_401(self, make_client):
        client = make_client()
        r = await client.get("/api/admin/users")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_platform_stats_requires_admin(self, make_client):
        """全站统计接口曾经只要登录就能看——这是既有的越权缺陷。"""
        _, bob = await make_admin_and_user(make_client)
        r = await bob.get("/api/admin/platform-stats")
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_page_renders(self, make_client):
        admin, _ = await make_admin_and_user(make_client)
        r = await admin.get("/web/admin/users")
        assert r.status_code == 200, r.text[:600]
        assert "用户管理" in r.text
        assert "alice" in r.text
        assert "bob" in r.text

    @pytest.mark.asyncio
    async def test_normal_user_page_forbidden(self, make_client):
        _, bob = await make_admin_and_user(make_client)
        r = await bob.get("/web/admin/users")
        assert r.status_code == 403


class TestOverviewIsCrossTenant:
    @pytest.mark.asyncio
    async def test_counts_cover_all_users(self, make_client):
        """管理员自己是租户上下文，统计必须显式跨租户。"""
        admin, bob = await make_admin_and_user(make_client)

        # 两个人各加一个站点
        r = await admin.post(
            "/api/sites", json={"name": "Alice站", "url": "https://alice.example"}
        )
        assert r.status_code == 201, r.text
        r = await bob.post(
            "/api/sites", json={"name": "Bob站", "url": "https://bob.example"}
        )
        assert r.status_code == 201, r.text

        overview = (await admin.get("/api/admin/users/overview")).json()
        assert overview["users_total"] == 2
        assert overview["sites_total"] == 2, "管理员必须看到全站友链，不只是自己的"
        assert overview["users_admin"] == 1

    @pytest.mark.asyncio
    async def test_per_user_counts_are_correct(self, make_client):
        admin, bob = await make_admin_and_user(make_client)
        await admin.post(
            "/api/sites", json={"name": "Alice站", "url": "https://alice.example"}
        )
        await bob.post(
            "/api/sites", json={"name": "Bob站", "url": "https://bob.example"}
        )

        body = (await admin.get("/api/admin/users")).json()
        by_name = {u["username"]: u for u in body["items"]}
        assert by_name["alice"]["site_count"] == 1
        assert by_name["bob"]["site_count"] == 1


class TestUserStateChanges:
    @pytest.mark.asyncio
    async def test_disable_user_blocks_login(self, make_client):
        admin, bob = await make_admin_and_user(make_client)
        bob_id = (await admin.get("/api/admin/users")).json()["items"]
        bob_id = next(u["id"] for u in bob_id if u["username"] == "bob")

        r = await admin.patch(f"/api/admin/users/{bob_id}", json={"is_active": False})
        assert r.status_code == 200, r.text
        assert r.json()["is_active"] is False

        fresh = make_client()
        r = await fresh.post(
            "/api/auth/login", json={"username": "bob", "password": PASSWORD}
        )
        assert r.status_code == 403, "被禁用的账号不能登录"

    @pytest.mark.asyncio
    async def test_disable_kills_existing_session(self, make_client):
        """已登录的会话也必须立刻失效——否则「禁用」只挡住了下一次登录。"""
        admin, bob = await make_admin_and_user(make_client)
        users = (await admin.get("/api/admin/users")).json()["items"]
        bob_id = next(u["id"] for u in users if u["username"] == "bob")

        assert (await bob.get("/api/users/me/cron-presets")).status_code == 200

        await admin.patch(f"/api/admin/users/{bob_id}", json={"is_active": False})

        r = await bob.get("/api/users/me/cron-presets")
        assert r.status_code == 401, "禁用后旧会话必须失效"

    @pytest.mark.asyncio
    async def test_promote_and_demote_admin(self, make_client):
        admin, bob = await make_admin_and_user(make_client)
        users = (await admin.get("/api/admin/users")).json()["items"]
        bob_id = next(u["id"] for u in users if u["username"] == "bob")

        r = await admin.patch(f"/api/admin/users/{bob_id}", json={"is_admin": True})
        assert r.status_code == 200
        assert r.json()["is_admin"] is True

        # 提权后 bob 能进管理页
        assert (await bob.get("/api/admin/users")).status_code == 200

        r = await admin.patch(f"/api/admin/users/{bob_id}", json={"is_admin": False})
        assert r.status_code == 200
        assert (await bob.get("/api/admin/users")).status_code == 403

    @pytest.mark.asyncio
    async def test_admin_note_persists(self, make_client):
        admin, _ = await make_admin_and_user(make_client)
        users = (await admin.get("/api/admin/users")).json()["items"]
        bob_id = next(u["id"] for u in users if u["username"] == "bob")

        r = await admin.patch(
            f"/api/admin/users/{bob_id}", json={"admin_note": "工单 #123 核验中"}
        )
        assert r.status_code == 200
        assert r.json()["admin_note"] == "工单 #123 核验中"


class TestSelfProtection:
    """管理员不能把自己锁在门外。"""

    @pytest.mark.asyncio
    async def test_cannot_disable_self(self, make_client):
        admin, _ = await make_admin_and_user(make_client)
        me = (await admin.get("/api/auth/me")).json()

        r = await admin.patch(f"/api/admin/users/{me['id']}", json={"is_active": False})
        assert r.status_code == 400
        assert "自己" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_cannot_demote_self(self, make_client):
        admin, _ = await make_admin_and_user(make_client)
        me = (await admin.get("/api/auth/me")).json()

        r = await admin.patch(f"/api/admin/users/{me['id']}", json={"is_admin": False})
        assert r.status_code == 400
        assert "自己" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_cannot_delete_self(self, make_client):
        admin, _ = await make_admin_and_user(make_client)
        me = (await admin.get("/api/auth/me")).json()

        r = await admin.delete(f"/api/admin/users/{me['id']}?confirm=true")
        assert r.status_code == 400


class TestPasswordReset:
    @pytest.mark.asyncio
    async def test_reset_changes_password(self, make_client):
        admin, _ = await make_admin_and_user(make_client)
        users = (await admin.get("/api/admin/users")).json()["items"]
        bob_id = next(u["id"] for u in users if u["username"] == "bob")

        r = await admin.post(
            f"/api/admin/users/{bob_id}/password",
            json={"new_password": "brand-new-pass-9"},
        )
        assert r.status_code == 200, r.text

        fresh = make_client()
        r = await fresh.post(
            "/api/auth/login",
            json={"username": "bob", "password": "brand-new-pass-9"},
        )
        assert r.status_code == 200

        other = make_client()
        r = await other.post(
            "/api/auth/login", json={"username": "bob", "password": PASSWORD}
        )
        assert r.status_code == 401, "旧密码必须失效"

    @pytest.mark.asyncio
    async def test_reset_invalidates_old_sessions(self, make_client):
        admin, bob = await make_admin_and_user(make_client)
        users = (await admin.get("/api/admin/users")).json()["items"]
        bob_id = next(u["id"] for u in users if u["username"] == "bob")

        assert (await bob.get("/api/users/me/cron-presets")).status_code == 200

        await admin.post(
            f"/api/admin/users/{bob_id}/password",
            json={"new_password": "brand-new-pass-9"},
        )

        r = await bob.get("/api/users/me/cron-presets")
        assert r.status_code == 401, "重置密码后旧会话必须失效"

    @pytest.mark.asyncio
    async def test_short_password_rejected(self, make_client):
        admin, _ = await make_admin_and_user(make_client)
        users = (await admin.get("/api/admin/users")).json()["items"]
        bob_id = next(u["id"] for u in users if u["username"] == "bob")

        r = await admin.post(
            f"/api/admin/users/{bob_id}/password", json={"new_password": "short"}
        )
        assert r.status_code == 422


class TestDeleteUser:
    @pytest.mark.asyncio
    async def test_requires_confirm(self, make_client):
        admin, _ = await make_admin_and_user(make_client)
        users = (await admin.get("/api/admin/users")).json()["items"]
        bob_id = next(u["id"] for u in users if u["username"] == "bob")

        r = await admin.delete(f"/api/admin/users/{bob_id}")
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_delete_removes_data(self, make_client):
        admin, bob = await make_admin_and_user(make_client)
        await bob.post(
            "/api/sites", json={"name": "Bob站", "url": "https://bob.example"}
        )
        users = (await admin.get("/api/admin/users")).json()["items"]
        bob_id = next(u["id"] for u in users if u["username"] == "bob")

        r = await admin.delete(f"/api/admin/users/{bob_id}?confirm=true")
        assert r.status_code == 200, r.text

        body = (await admin.get("/api/admin/users")).json()
        assert [u["username"] for u in body["items"]] == ["alice"]

        overview = (await admin.get("/api/admin/users/overview")).json()
        assert overview["sites_total"] == 0, "删除用户必须连带清掉他的站点"


class TestFilters:
    @pytest.mark.asyncio
    async def test_search_by_username(self, make_client):
        admin, _ = await make_admin_and_user(make_client)
        body = (await admin.get("/api/admin/users?search=bo")).json()
        assert [u["username"] for u in body["items"]] == ["bob"]

    @pytest.mark.asyncio
    async def test_filter_admin_role(self, make_client):
        admin, _ = await make_admin_and_user(make_client)
        body = (await admin.get("/api/admin/users?role=admin")).json()
        assert [u["username"] for u in body["items"]] == ["alice"]

    @pytest.mark.asyncio
    async def test_filter_disabled(self, make_client):
        admin, _ = await make_admin_and_user(make_client)
        users = (await admin.get("/api/admin/users")).json()["items"]
        bob_id = next(u["id"] for u in users if u["username"] == "bob")
        await admin.patch(f"/api/admin/users/{bob_id}", json={"is_active": False})

        body = (await admin.get("/api/admin/users?status=disabled")).json()
        assert [u["username"] for u in body["items"]] == ["bob"]

    @pytest.mark.asyncio
    async def test_pagination(self, make_client):
        admin = make_client()
        await register(admin, "alice")
        for i in range(4):
            other = make_client()
            await register(other, f"user{i}")

        await login(admin, "alice")
        page1 = (await admin.get("/api/admin/users?limit=2&offset=0")).json()
        page2 = (await admin.get("/api/admin/users?limit=2&offset=2")).json()
        assert page1["total"] == 5
        assert len(page1["items"]) == 2
        assert len(page2["items"]) == 2
        assert {u["id"] for u in page1["items"]}.isdisjoint(
            {u["id"] for u in page2["items"]}
        )
