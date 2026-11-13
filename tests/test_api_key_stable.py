"""API Key 的稳定性与可查看性。

背景（v2.0.1）：线上出现「Key 无效」的反馈。排查结果是——

- 数据库里的 `api_key_hash` 完好，博客仓库里配的 Key 与它完全一致；
- 服务端连续 15 次带 Key 请求全部 200，鉴权本身没问题；
- 日志里没有任何「重新生成」请求，**Key 从未被改动过**。

真正的问题在设置页：`settings_page` 里写了
`ctx.update(api_key_display=new_key)`，`new_key` 来自一次性 Cookie，
普通访问时是空字符串 —— 于是页面永远显示「（暂无）」。
用户看到「暂无」会以为 Key 丢了，转而去点「重新生成」，
而重新生成必然改变 Key，博客前端立刻全线 401。

**「看不到」被这个设计逼成了「Key 变了」。**

所以这里守两组不变量：
1. Key 只在用户主动「重新生成」时才变，其它任何操作都不许动它；
2. 设置页必须能显示当前 Key；存量账号用「认领」补明文，Key 本身不变。
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
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


async def _register(client, username="alice", password="password123"):
    r = await client.post(
        "/api/auth/register",
        json={"username": username, "password": password},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _login(client, username="alice", password="password123"):
    r = await client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _read_user(username="alice"):
    """直接读库，拿 api_key_hash / api_key_plain。"""
    from app.db import get_session_factory
    from app.models import User

    factory = get_session_factory()
    async with factory() as session:
        row = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one()
        return row.api_key_hash, row.api_key_plain


class TestPlainKeyPersisted:
    @pytest.mark.asyncio
    async def test_register_stores_plain_key(self, client):
        """注册返回的 Key 必须同时落库，否则设置页看不到。"""
        data = await _register(client)
        _, plain = await _read_user()
        assert plain == data["api_key"]

    @pytest.mark.asyncio
    async def test_hash_matches_plain(self, client):
        """哈希与明文必须互相对应，否则鉴权和展示会打架。"""
        from app.core.security import hash_api_key

        data = await _register(client)
        key_hash, plain = await _read_user()
        assert hash_api_key(plain) == key_hash
        assert hash_api_key(data["api_key"]) == key_hash


class TestKeyStability:
    """Key 只在用户主动「重新生成」时才允许变化。"""

    @pytest.mark.asyncio
    async def test_login_does_not_change_key(self, client):
        await _register(client)
        before = await _read_user()
        await _login(client)
        after = await _read_user()
        assert before == after

    @pytest.mark.asyncio
    async def test_update_settings_does_not_change_key(self, client):
        """改 AI 配置、采集频率等都不许碰 Key。"""
        await _register(client)
        await _login(client)
        before = await _read_user()
        r = await client.patch(
            "/api/users/me",
            json={"ai_enabled": False, "crawl_cron": "0 4 * * *"},
        )
        assert r.status_code == 200, r.text
        after = await _read_user()
        assert before == after, "更新设置不应该改变 API Key"

    @pytest.mark.asyncio
    async def test_change_password_does_not_change_key(self, client):
        await _register(client)
        await _login(client)
        before = await _read_user()
        r = await client.post(
            "/api/users/me/password",
            json={"old_password": "password123", "new_password": "newpassword456"},
        )
        assert r.status_code == 200, r.text
        after = await _read_user()
        assert before == after, "改密码不应该改变 API Key"

    @pytest.mark.asyncio
    async def test_settings_page_render_does_not_change_key(self, client):
        """渲染设置页是读操作，绝不能顺带写回。"""
        await _register(client)
        await _login(client)
        before = await _read_user()
        r = await client.get("/web/settings")
        assert r.status_code == 200
        after = await _read_user()
        assert before == after


class TestSettingsPageShowsKey:
    @pytest.mark.asyncio
    async def test_settings_page_contains_full_key(self, client):
        """设置页必须能拿到完整 Key —— 这是本次修复的核心。"""
        data = await _register(client)
        await _login(client)
        r = await client.get("/web/settings")
        assert r.status_code == 200
        assert data["api_key"] in r.text

    @pytest.mark.asyncio
    async def test_key_input_defaults_to_masked(self, client):
        """默认打码，避免旁边的人直接看到。"""
        await _register(client)
        await _login(client)
        r = await client.get("/web/settings")
        assert 'id="key-box"' in r.text
        assert 'type="password"' in r.text

    @pytest.mark.asyncio
    async def test_import_prompt_hidden_when_plain_exists(self, client):
        """已有明文时不该再提示认领。"""
        await _register(client)
        await _login(client)
        r = await client.get("/web/settings")
        assert "认领你正在使用的 Key" not in r.text


class TestRegenerate:
    @pytest.mark.asyncio
    async def test_regenerate_changes_both(self, client):
        """重新生成时哈希与明文必须一起更新，否则页面显示旧值。"""
        old = await _register(client)
        await _login(client)
        r = await client.post("/api/users/me/api-key")
        assert r.status_code == 200
        new_key = r.json()["api_key"]
        assert new_key != old["api_key"]

        key_hash, plain = await _read_user()
        assert plain == new_key

    @pytest.mark.asyncio
    async def test_old_key_rejected_after_regenerate(self, client):
        old = await _register(client)
        await _login(client)
        await client.post("/api/users/me/api-key")

        fresh = AsyncClient(
            transport=ASGITransport(app=client._transport.app),
            base_url="http://test",
        )
        async with fresh:
            r = await fresh.get("/v1/stats", headers={"X-API-Key": old["api_key"]})
            assert r.status_code == 401


class TestImportExistingKey:
    """存量账号：明文丢失时用「认领」补上，Key 本身不变。"""

    @pytest.mark.asyncio
    async def test_import_correct_key_succeeds_without_changing_hash(self, client):
        data = await _register(client)
        await _login(client)
        before_hash, _ = await _read_user()

        # 模拟存量账号：明文丢失
        from app.db import get_session_factory
        from app.models import User

        factory = get_session_factory()
        async with factory() as session:
            row = (
                await session.execute(select(User).where(User.username == "alice"))
            ).scalar_one()
            row.api_key_plain = None
            await session.commit()

        r = await client.post(
            "/api/users/me/api-key/import", json={"api_key": data["api_key"]}
        )
        assert r.status_code == 200, r.text

        after_hash, plain = await _read_user()
        assert plain == data["api_key"]
        # 关键：hash 完全没动，所以 Key 与博客配置都不需要改
        assert after_hash == before_hash

    @pytest.mark.asyncio
    async def test_import_wrong_key_rejected(self, client):
        await _register(client)
        await _login(client)
        r = await client.post(
            "/api/users/me/api-key/import",
            json={"api_key": "fc_" + "0" * 32},
        )
        assert r.status_code == 400
        assert "不匹配" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_import_then_visible_in_settings(self, client):
        data = await _register(client)
        await _login(client)

        from app.db import get_session_factory
        from app.models import User

        factory = get_session_factory()
        async with factory() as session:
            row = (
                await session.execute(select(User).where(User.username == "alice"))
            ).scalar_one()
            row.api_key_plain = None
            await session.commit()

        # 认领前：显示导入引导
        r = await client.get("/web/settings")
        assert "认领你正在使用的 Key" in r.text

        await client.post(
            "/api/users/me/api-key/import", json={"api_key": data["api_key"]}
        )

        # 认领后：直接显示 Key
        r = await client.get("/web/settings")
        assert data["api_key"] in r.text
        assert "认领你正在使用的 Key" not in r.text

    @pytest.mark.asyncio
    async def test_import_requires_login(self, client):
        r = await client.post(
            "/api/users/me/api-key/import", json={"api_key": "fc_" + "0" * 32}
        )
        assert r.status_code in (401, 403)


class TestModelAndMigration:
    def test_model_has_plain_column(self):
        from app.models.user import User

        assert "api_key_plain" in User.__table__.columns

    def test_migration_exists(self):
        from pathlib import Path

        path = Path("alembic/versions/0011_user_api_key_plain.py")
        assert path.exists()
        assert "api_key_plain" in path.read_text(encoding="utf-8")

    def test_settings_template_no_longer_hardcodes_empty(self):
        """回归：设置页不能再用「（暂无）」当唯一状态。

        曾经 ctx.update(api_key_display=new_key) 无条件覆盖成空字符串，
        页面永远显示「（暂无）」。
        """
        from pathlib import Path

        content = Path("web/templates/settings.html").read_text(encoding="utf-8")
        assert "api_key_needs_import" in content
        assert "认领你正在使用的 Key" in content
