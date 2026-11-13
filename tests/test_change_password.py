"""修改密码功能。

两条要求：
1. 设置页能改密码
2. **改密后旧会话全部失效**——Session 是无状态签名，
   没法主动踢人下线，只能靠「签发时间早于改密时间」这个判断挡住旧 Cookie。

如果只做 1 不做 2，用户改完密码，
别人拿着旧浏览器（或 XSS 偷到的 Cookie）还能继续用一周——等于没改。
"""

import time

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


async def _register(client, username="alice", password="password123"):
    r = await client.post(
        "/api/auth/register",
        json={"username": username, "password": password},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _login(client, username="alice", password="password123"):
    """登录并保留 Cookie（注册接口不自动登录）。"""
    r = await client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert r.status_code == 200, r.text
    return r.json()


class TestChangePasswordApi:
    @pytest.mark.asyncio
    async def test_change_password_succeeds(self, client):
        await _register(client)
        await _login(client)
        r = await client.post(
            "/api/users/me/password",
            json={"old_password": "password123", "new_password": "newpassword456"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True

    @pytest.mark.asyncio
    async def test_old_password_wrong_rejected(self, client):
        await _register(client)
        await _login(client)
        r = await client.post(
            "/api/users/me/password",
            json={"old_password": "wrongpassword", "new_password": "newpassword456"},
        )
        assert r.status_code == 400
        assert "原密码" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_new_password_must_differ(self, client):
        await _register(client)
        await _login(client)
        r = await client.post(
            "/api/users/me/password",
            json={"old_password": "password123", "new_password": "password123"},
        )
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_new_password_too_short(self, client):
        await _register(client)
        await _login(client)
        r = await client.post(
            "/api/users/me/password",
            json={"old_password": "password123", "new_password": "short"},
        )
        assert r.status_code == 422  # pydantic 校验拦下

    @pytest.mark.asyncio
    async def test_requires_login(self, client):
        r = await client.post(
            "/api/users/me/password",
            json={"old_password": "password123", "new_password": "newpassword456"},
        )
        assert r.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_new_password_works_for_login(self, client):
        """改完密码后能用新密码登录。"""
        await _register(client)
        await _login(client)
        await client.post(
            "/api/users/me/password",
            json={"old_password": "password123", "new_password": "newpassword456"},
        )
        # 换一个新客户端，避免受当前会话影响
        async with AsyncClient(
            transport=ASGITransport(app=client._transport.app),
            base_url="http://test",
        ) as fresh:
            r = await fresh.post(
                "/api/auth/login",
                json={"username": "alice", "password": "newpassword456"},
            )
            assert r.status_code == 200, r.text

    @pytest.mark.asyncio
    async def test_old_password_no_longer_works(self, client):
        """改完密码后旧密码不能再登录。"""
        await _register(client)
        await _login(client)
        await client.post(
            "/api/users/me/password",
            json={"old_password": "password123", "new_password": "newpassword456"},
        )
        async with AsyncClient(
            transport=ASGITransport(app=client._transport.app),
            base_url="http://test",
        ) as fresh:
            r = await fresh.post(
                "/api/auth/login",
                json={"username": "alice", "password": "password123"},
            )
            assert r.status_code == 401


class TestOldSessionsInvalidated:
    """改密后旧会话必须失效。"""

    @pytest.mark.asyncio
    async def test_old_cookie_rejected_after_change(self, client):
        """关键用例：改密码前签发的 Cookie，改密码后必须被拒。

        用**独立客户端**模拟「另一台设备」——
        同一个 client 的 cookie jar 会被改密响应里的 Set-Cookie 覆盖，
        测不出旧会话有没有失效。
        """
        await _register(client)
        await _login(client)

        # 造一个「改密前」签发的会话，放在另一个客户端里
        from app.core.session import SESSION_COOKIE, write_session

        old_token = write_session(1)

        # 改密（用当前客户端，会拿到新 Cookie）
        r = await client.post(
            "/api/users/me/password",
            json={"old_password": "password123", "new_password": "newpassword456"},
        )
        assert r.status_code == 200

        # 另一个客户端拿着改密前的 Cookie —— 应该被拒
        async with AsyncClient(
            transport=ASGITransport(app=client._transport.app),
            base_url="http://test",
            cookies={SESSION_COOKIE: old_token},
        ) as other:
            r = await other.get("/api/users/me/cron-presets")
            assert r.status_code == 401, "旧会话应该失效"

    @pytest.mark.asyncio
    async def test_current_session_still_valid(self, client):
        """改密后当前设备不受影响——响应里换发了新 Cookie。"""
        await _register(client)
        await _login(client)

        r = await client.post(
            "/api/users/me/password",
            json={"old_password": "password123", "new_password": "newpassword456"},
        )
        assert r.status_code == 200
        assert "Set-Cookie" in r.headers

        # 客户端自动用上了新 Cookie，还能访问
        r = await client.get("/api/users/me/cron-presets")
        assert r.status_code == 200, "当前设备应该还能用"

    def test_session_contains_issued_at(self):
        """会话里必须带签发时间，否则没法判断新旧。"""
        from app.core.session import read_session_data, write_session

        token = write_session(1)
        data = read_session_data(token)
        assert data is not None
        assert isinstance(data["uid"], int)
        assert isinstance(data["iat"], int)
        assert abs(data["iat"] - int(time.time())) < 60

    def test_new_session_issued_after_password_change(self):
        """改密换发的新会话，签发时间必须严格晚于改密时刻。

        校验规则是 issued_at > changed_at——
        同一秒签发的新会话会被当成旧会话挡掉。
        """
        from app.core.session import read_session_data, write_session

        changed_at = int(time.time())
        token = write_session(1, issued_at=changed_at + 1)
        data = read_session_data(token)
        assert data["iat"] > changed_at


class TestPasswordChangedAt:
    def test_model_has_field(self):
        from app.models.user import User

        assert "password_changed_at" in User.__table__.columns

    def test_migration_exists(self):
        from pathlib import Path

        path = Path("alembic/versions/0009_user_password_changed_at.py")
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "password_changed_at" in content
        # 必须回填，不能留 NULL——为 NULL 会跳过时间比较
        assert "UPDATE users" in content


class TestSettingsPage:
    def test_settings_page_has_password_form(self):
        from pathlib import Path

        content = Path("web/templates/settings.html").read_text(encoding="utf-8")
        assert "pw-old" in content
        assert "pw-new" in content
        assert "pw-confirm" in content
        assert "changePassword" in content
        assert "/api/users/me/password" in content

    def test_form_confirms_password_match(self):
        """前端要检查两次输入一致。"""
        from pathlib import Path

        content = Path("web/templates/settings.html").read_text(encoding="utf-8")
        assert "pw-confirm" in content
        assert "不一致" in content
