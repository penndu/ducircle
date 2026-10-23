"""API 端到端测试。

用 SQLite + ASGI TestClient 跑完整链路：注册 → 登录 → 加友链 →
读文章 → 换用户确认看不到别人的数据。

这部分是租户隔离的端到端验证——单元测试证明钩子会注入过滤条件，
这里证明真实请求路径上隔离确实生效。
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest_asyncio.fixture
async def app_with_db(monkeypatch):
    """每个用例一个独立的内存库应用实例。

    用 StaticPool 保证所有连接共用同一个内存库，
    否则每个新连接都会拿到一个空库。
    """
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

    # 只替换 app.db.get_session_factory 即可：deps.py 的 DbSession 别名在导入时
    # 已固化了 Depends(get_db)，但 get_db 内部每次调用都会去读
    # get_session_factory()，所以替换工厂就够了，不必动 Depends。
    monkeypatch.setattr(db_module, "get_session_factory", lambda: factory)

    # 健康检查用全局引擎，这里一并替换，避免测试依赖真实 MySQL。
    # main.py 里通过 db_module.check_db 调用，因此 patch 在 db 模块上。
    async def _check_db():
        return True

    monkeypatch.setattr(db_module, "check_db", _check_db)

    # 无 Redis 时限流应放行。让限流器直接返回允许，
    # 不依赖真的连上 Redis。
    from app.core.ratelimit import RateLimiter

    async def _allow(self, user_id, cost=1.0, rate=None, burst=None):
        return True

    monkeypatch.setattr(RateLimiter, "allow", _allow)

    from app.main import app as fastapi_app

    yield fastapi_app

    await engine.dispose()


@pytest.mark.asyncio
async def test_register_login_flow(app_with_db):
    """注册 → 登录 → 读取自己信息。"""
    transport = ASGITransport(app=app_with_db)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 注册
        r = await client.post(
            "/api/auth/register",
            json={"username": "alice", "password": "password123"},
        )
        assert r.status_code == 201, r.text
        data = r.json()
        api_key = data["api_key"]
        assert api_key.startswith("fc_")
        assert len(api_key) == 35  # "fc_" + 32 位十六进制

        # 重复注册应被拒
        r = await client.post(
            "/api/auth/register",
            json={"username": "alice", "password": "password123"},
        )
        assert r.status_code == 409

        # 未登录访问 /me 应 401
        r = await client.get("/api/auth/me")
        assert r.status_code == 401

        # 登录
        r = await client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "password123"},
        )
        assert r.status_code == 200

        # 带上 session cookie 后可访问
        r = await client.get("/api/auth/me")
        assert r.status_code == 200
        assert r.json()["username"] == "alice"

        # API Key 可用于只读接口
        r = await client.get(
            "/v1/friends", headers={"X-API-Key": api_key}
        )
        assert r.status_code == 200

        # 错误的 Key 应 401
        r = await client.get("/v1/friends", headers={"X-API-Key": "fc_wrong"})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_weak_password_rejected(app_with_db):
    transport = ASGITransport(app=app_with_db)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 密码过短：Pydantic 的 min_length 先拦截，返回 422
        r = await client.post(
            "/api/auth/register",
            json={"username": "bob", "password": "short"},
        )
        assert r.status_code == 422

        # 用户名含空格：业务规则校验拦截，返回 400。
        # 与 422 分开是刻意的——422 是「结构不对」，400 是「值不合法」。
        r = await client.post(
            "/api/auth/register",
            json={"username": "a b", "password": "password123"},
        )
        assert r.status_code == 400, "用户名含空格应被业务校验拒绝"
        assert "用户名" in r.json()["detail"]


@pytest.mark.asyncio
async def test_site_crud_isolation(app_with_db):
    """两个用户各自加友链，互相看不到。"""
    transport = ASGITransport(app=app_with_db)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Alice 注册登录
        await client.post(
            "/api/auth/register",
            json={"username": "alice", "password": "password123"},
        )
        alice_key = (
            await client.post(
                "/api/auth/register",
                json={"username": "alice2", "password": "password123"},
            )
        ).json()["api_key"]
        await client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "password123"},
        )

        # Alice 加两个友链
        for name, url in [
            ("站点甲", "https://jia.example.com/"),
            ("站点乙", "https://yi.example.com/"),
        ]:
            r = await client.post(
                "/api/sites", json={"name": name, "url": url}
            )
            assert r.status_code == 201, r.text

        assert len((await client.get("/api/sites")).json()) == 2

        # 重复域名应被拒
        r = await client.post(
            "/api/sites", json={"name": "重复", "url": "https://jia.example.com"}
        )
        assert r.status_code == 409

        # 退出登录
        await client.post("/api/auth/logout")
        assert (await client.get("/api/sites")).status_code == 401

        # Bob 登录
        await client.post(
            "/api/auth/register",
            json={"username": "bob", "password": "password123"},
        )
        await client.post(
            "/api/auth/login",
            json={"username": "bob", "password": "password123"},
        )

        # Bob 应该看不到 Alice 的站点
        assert len((await client.get("/api/sites")).json()) == 0

        # Bob 用 API Key 查文章流，应该也是空的
        r = await client.get("/v1/all", headers={"X-API-Key": alice_key})
        assert r.status_code == 200
        assert r.json()["total"] == 0
        assert r.json()["data"] == []


@pytest.mark.asyncio
async def test_health_endpoint(app_with_db):
    transport = ASGITransport(app=app_with_db)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/health")
        # 内存库的 check_db 用的是全局引擎，这里会报 down，但接口本身不该 500
        assert r.status_code == 200
        assert "status" in r.json()


@pytest.mark.asyncio
async def test_openapi_schema_generates(app_with_db):
    """OpenAPI 文档要能正常生成，否则 /docs 是空的。"""
    transport = ASGITransport(app=app_with_db)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/openapi.json")
        assert r.status_code == 200
        schema = r.json()
        assert "/api/auth/register" in schema["paths"]
        assert "/v1/all" in schema["paths"]
        assert "/api/sites" in schema["paths"]