"""自助注册的开关与首次登录体验。

两件事必须成立：

1. ``ALLOW_REGISTRATION=false`` 时，页面和接口**都**要拒绝——
   只在前端隐藏表单的话，直接 POST 照样能建号。
2. 注册成功后用户必须能**看到自己的 API Key**。
   数据库只存哈希，重定向到概览页的话明文就永久丢了，
   新用户只能去点「重新生成」——那是个没必要的坑。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

PASSWORD = "password123"


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
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        yield c
    await engine.dispose()


@pytest.fixture
def closed_registration(monkeypatch):
    """把平台切到「不开放注册」。"""
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "allow_registration", False)
    return settings


class TestRegistrationOpenByDefault:
    @pytest.mark.asyncio
    async def test_register_page_shows_form(self, client):
        r = await client.get("/web/register")
        assert r.status_code == 200
        assert "注册" in r.text
        assert "用户名" in r.text

    @pytest.mark.asyncio
    async def test_api_register_works(self, client):
        r = await client.post(
            "/api/auth/register",
            json={"username": "newbie", "password": PASSWORD},
        )
        assert r.status_code == 201


class TestRegistrationClosed:
    @pytest.mark.asyncio
    async def test_page_explains_closure(self, client, closed_registration):
        r = await client.get("/web/register")
        assert r.status_code == 200
        assert "暂不开放" in r.text

    @pytest.mark.asyncio
    async def test_form_submit_rejected(self, client, closed_registration):
        r = await client.post(
            "/web/register",
            data={"username": "newbie", "password": PASSWORD, "email": ""},
        )
        assert r.status_code == 403
        assert "暂不开放" in r.text

    @pytest.mark.asyncio
    async def test_api_register_rejected(self, client, closed_registration):
        """前端藏了表单不等于接口关了——必须服务端也拦。"""
        r = await client.post(
            "/api/auth/register",
            json={"username": "newbie", "password": PASSWORD},
        )
        assert r.status_code == 403
        assert "暂不开放" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_no_account_created(self, client, closed_registration):
        await client.post(
            "/api/auth/register",
            json={"username": "newbie", "password": PASSWORD},
        )
        r = await client.post(
            "/api/auth/login", json={"username": "newbie", "password": PASSWORD}
        )
        assert r.status_code == 401, "被拒绝的注册不该留下账号"


class TestApiKeyVisibility:
    @pytest.mark.asyncio
    async def test_register_redirects_to_settings_with_key(self, client):
        from app.core.session import NEW_KEY_COOKIE

        r = await client.post(
            "/web/register",
            data={"username": "alice", "password": PASSWORD, "email": ""},
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/web/settings"

        # 客户端自动保存了 Cookie
        assert NEW_KEY_COOKIE in client.cookies
        assert client.cookies[NEW_KEY_COOKIE].startswith("fc_")

    @pytest.mark.asyncio
    async def test_settings_page_shows_key_persistently(self, client):
        """设置页随时都能看到明文 Key（v2.0.1 起）。

        以前只在注册后那一次显示，之后永远显示「（暂无）」。
        用户看到「暂无」会以为 Key 丢了，转而去点「重新生成」，
        而重新生成必然改变 Key ——「看不到」被逼成了「Key 变了」。
        """
        from app.core.session import NEW_KEY_COOKIE

        r = await client.post(
            "/web/register",
            data={"username": "alice", "password": PASSWORD, "email": ""},
        )
        assert r.status_code == 303
        key = client.cookies[NEW_KEY_COOKIE]

        r = await client.get("/web/settings")
        assert r.status_code == 200
        assert key in r.text, "设置页必须把明文 Key 显示出来"

        # 再访问一次仍然看得到：明文已落库，不再依赖一次性 Cookie
        r = await client.get("/web/settings")
        assert key in r.text, "重复访问设置页也应该能看到 Key"

    @pytest.mark.asyncio
    async def test_key_is_stored_hashed(self, client):
        """鉴权字段是哈希，不是明文。

        v2.0.1 起另外存了一份明文副本（api_key_plain），
        但那只是为了设置页能显示；实际校验始终比对 api_key_hash。
        """
        from sqlalchemy import select

        import app.db as db_module
        from app.models import User

        r = await client.post(
            "/web/register",
            data={"username": "alice", "password": PASSWORD, "email": ""},
        )
        assert r.status_code == 303
        raw_key = client.cookies["ducircle_new_key"]

        factory = db_module.get_session_factory()
        async with factory() as session:
            user = (await session.execute(select(User))).scalars().first()
            assert user is not None
            assert user.api_key_hash != raw_key
            assert raw_key not in (user.api_key_hash or "")
            # 明文副本与鉴权字段分开存放，且确实是这一把 Key
            assert user.api_key_plain == raw_key
