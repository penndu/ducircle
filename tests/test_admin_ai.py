"""平台 AI 接口设置：权限、脱敏、主备切换、.env 回退。

关键不变量：
1. 普通用户碰不到管理接口（403）。
2. GET 绝不返回完整 Key。
3. 库里没配置时平台模式行为与升级前完全一致。
4. 主失败自动切备，且只切可恢复错误。
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


class TestAdminAIApi:
    @pytest.mark.asyncio
    async def test_requires_admin(self, make_client):
        anon = make_client()
        assert (await anon.get("/api/admin/ai/providers")).status_code == 401

        await _register_login(make_client(), "alice")  # 首个账号自动管理员
        bob = make_client()
        await _register_login(bob, "bob")
        assert (await bob.get("/api/admin/ai/providers")).status_code == 403
        assert (await bob.put("/api/admin/ai/providers/primary", json={})).status_code == 403

    @pytest.mark.asyncio
    async def test_get_initial_state_falls_back_to_env(self, make_client):
        admin = make_client()
        await _register_login(admin, "alice")
        r = await admin.get("/api/admin/ai/providers")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["source"] == "env"
        assert [i["slot"] for i in data["items"]] == ["primary", "backup"]
        assert data["providers"], "预置服务商清单要给下拉框用"

    @pytest.mark.asyncio
    async def test_save_masks_key_and_keeps_on_empty(self, make_client):
        from app.api.admin_ai import mask_key

        admin = make_client()
        await _register_login(admin, "alice")

        r = await admin.put(
            "/api/admin/ai/providers/primary",
            json={
                "provider": "minimax_cn",
                "base_url": "https://api.minimaxi.com/v1",
                "model": "MiniMax-M3.1-Flash",
                "api_key": "sk-abcdefgh1234",
                "enabled": True,
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["has_key"] is True
        assert r.json()["key_masked"] == mask_key("sk-abcdefgh1234")

        # 再 GET：绝不能出现完整 Key
        data = (await admin.get("/api/admin/ai/providers")).json()
        assert data["source"] == "db"
        assert "sk-abcdefgh1234" not in str(data)
        assert data["items"][0]["key_masked"] == mask_key("sk-abcdefgh1234")

        # 只改模型，Key 留空 → 保留原值
        r = await admin.put(
            "/api/admin/ai/providers/primary",
            json={"model": "MiniMax-Other", "enabled": True},
        )
        assert r.status_code == 200, r.text
        data = (await admin.get("/api/admin/ai/providers")).json()
        assert data["items"][0]["model"] == "MiniMax-Other"
        assert data["items"][0]["key_masked"] == mask_key("sk-abcdefgh1234")

    @pytest.mark.asyncio
    async def test_enable_requires_key_and_url(self, make_client):
        admin = make_client()
        await _register_login(admin, "alice")
        r = await admin.put(
            "/api/admin/ai/providers/backup", json={"enabled": True}
        )
        assert r.status_code == 400
        assert "API Key" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_clear_key_explicit(self, make_client):
        admin = make_client()
        await _register_login(admin, "alice")
        await admin.put(
            "/api/admin/ai/providers/primary",
            json={
                "provider": "minimax_cn",
                "api_key": "sk-abcdefgh1234",
                "enabled": True,
            },
        )
        r = await admin.put(
            "/api/admin/ai/providers/primary",
            json={"clear_key": True, "enabled": False},
        )
        assert r.status_code == 200, r.text
        data = (await admin.get("/api/admin/ai/providers")).json()
        assert data["items"][0]["has_key"] is False
        assert data["source"] == "env"

    @pytest.mark.asyncio
    async def test_admin_page_renders(self, make_client):
        admin = make_client()
        await _register_login(admin, "alice")
        r = await admin.get("/web/admin/ai")
        assert r.status_code == 200, r.text[:400]
        assert "AI 接口设置" in r.text
        assert "主 API" in r.text and "备 API" in r.text
        assert "AI 接口设置" in r.text  # Tab

    @pytest.mark.asyncio
    async def test_admin_home_accepts_ai_tab(self, make_client):
        admin = make_client()
        await _register_login(admin, "alice")
        r = await admin.get("/web/admin?tab=ai", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/web/admin/ai"


class TestPlatformFailover:
    @pytest.mark.asyncio
    async def test_db_config_takes_over_env(self, app_env):
        _, factory = app_env
        from app.models import AIProviderConfig
        from app.services.platform_ai import load_platform_clients

        async with factory() as db:
            db.add(
                AIProviderConfig(
                    slot="primary",
                    provider="minimax_cn",
                    base_url="https://api.minimaxi.com/v1",
                    api_key="sk-db-primary",
                    model="MiniMax-M3.1-Flash",
                    style="openai",
                    enabled=True,
                )
            )
            await db.flush()
            clients, source = await load_platform_clients(db)
            assert source == "db"
            assert len(clients) == 1
            assert clients[0].client.api_key == "sk-db-primary"
            assert clients[0].client.model == "MiniMax-M3.1-Flash"

    @pytest.mark.asyncio
    async def test_fallback_to_env_when_no_db_config(self, app_env):
        _, factory = app_env
        from app.services.platform_ai import load_platform_clients

        async with factory() as db:
            clients, source = await load_platform_clients(db)
            assert source == "env"
            assert len(clients) == 1

    @pytest.mark.asyncio
    async def test_failover_on_retryable_error(self, app_env):
        from app.services.llm import LLMError
        from app.services.platform_ai import PlatformClient, complete_on_platform

        class _Fail:
            model = "m1"
            style = "openai"

            async def complete(self, *a, **kw):
                raise LLMError("触发服务商限流", retryable=True)

        class _Ok:
            model = "m2"
            style = "openai"

            async def complete(self, *a, **kw):
                return '{"ok": true}'

        clients = [
            PlatformClient(_Fail(), "primary", "主 API"),
            PlatformClient(_Ok(), "backup", "备 API"),
        ]
        raw, used = await complete_on_platform(clients, "hi")
        assert raw == '{"ok": true}'
        assert used.slot == "backup"

    @pytest.mark.asyncio
    async def test_no_failover_on_permanent_error(self, app_env):
        from app.services.llm import LLMError
        from app.services.platform_ai import PlatformClient, complete_on_platform

        calls: list[str] = []

        class _Permanent:
            model = "m1"

            async def complete(self, *a, **kw):
                calls.append("primary")
                raise LLMError("内容安全拦截", retryable=False, permanent=True)

        class _Never:
            model = "m2"

            async def complete(self, *a, **kw):
                calls.append("backup")
                return "ok"

        with pytest.raises(LLMError):
            await complete_on_platform(
                [
                    PlatformClient(_Permanent(), "primary", "主 API"),
                    PlatformClient(_Never(), "backup", "备 API"),
                ],
                "hi",
            )
        assert calls == ["primary"], "永久错误不该再试备 API"
