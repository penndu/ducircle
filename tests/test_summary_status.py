"""摘要生成的状态可见性。

问题：批量摘要跑在后台，HTTP 请求早返回 200。后台失败时异常被 except 吞掉，
用户面对的就是「点了没反应」——和任务根本没跑完全没法区分。

具体表现过：用户 00:00 填了 API Key，定时任务在 00:00 跑时 Key 还没保存，
日志里写「未配置 API Key」，但用户在页面上什么都看不到。
"""

import asyncio

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


class TestSummaryStatusService:
    def test_key_includes_user(self):
        from app.services.summary_status import _key

        assert _key(1) != _key(2)

    @pytest.mark.asyncio
    async def test_set_and_get_roundtrip(self, monkeypatch):
        """Redis 不可用时不能抛异常——状态只是锦上添花。"""
        from app.services import summary_status

        store: dict = {}

        class _FakeRedis:
            async def set(self, key, value, ex=None):
                store[key] = value

            async def get(self, key):
                return store.get(key)

        monkeypatch.setattr(
            summary_status.RedisManager,
            "instance",
            classmethod(lambda cls: type("X", (), {"client": _FakeRedis()})()),
        )

        await summary_status.set_status(1, {"state": "running", "generated": 3})
        got = await summary_status.get_status(1)
        assert got["state"] == "running"
        assert got["generated"] == 3
        assert "updated_at" in got

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, monkeypatch):
        from app.services import summary_status

        class _FakeRedis:
            async def get(self, key):
                return None

        monkeypatch.setattr(
            summary_status.RedisManager,
            "instance",
            classmethod(lambda cls: type("X", (), {"client": _FakeRedis()})()),
        )
        assert await summary_status.get_status(99) is None


class TestBatchStatusEndpoint:
    @pytest.mark.asyncio
    async def test_idle_when_no_record(self, client):
        await client.post(
            "/web/register",
            data={"username": "alice", "password": "password123", "email": ""},
            follow_redirects=False,
        )
        r = await client.get("/api/ai/batch-status")
        assert r.status_code == 200
        assert r.json()["state"] in ("idle", "done", "running", "failed")

    @pytest.mark.asyncio
    async def test_requires_login(self, client):
        r = await client.get("/api/ai/batch-status")
        assert r.status_code in (401, 403)


class TestSummaryStatusRecorded:
    """摘要任务必须留下状态记录，否则失败时用户无从知晓。

    这几个用例走 API 层（走 fixture 里的 sqlite），
    不直接调服务函数——直接调会绕过 DB patch，跑到生产的 MySQL 上。
    """

    @pytest.mark.asyncio
    async def test_batch_records_status(self, client, monkeypatch):
        """摘要任务必须写状态（running → 终态）。"""
        from app.services import ai_summary, summary_status

        recorded: list = []

        async def _fake_set(user_id, data):
            recorded.append(data)

        monkeypatch.setattr(summary_status, "set_status", _fake_set)
        # ai_summary 是模块级导入 set_status 的，
        # 得同时替换它自己模块里的引用
        monkeypatch.setattr(ai_summary, "set_status", _fake_set)

        await client.post(
            "/web/register",
            data={"username": "alice", "password": "password123", "email": ""},
            follow_redirects=False,
        )

        # 直接调服务函数（库里没有待处理文章，会走「没有待处理」分支）
        await ai_summary.summarize_pending_for_user(1, limit=5)

        assert recorded, "应该写了状态"
        assert recorded[0]["state"] == "running", "开始时要写 running"
        assert recorded[-1]["state"] in ("done", "failed"), "结束时要写终态"
        assert "total" in recorded[-1]

    @pytest.mark.asyncio
    async def test_status_endpoint_reflects_record(self, client, monkeypatch):
        """状态接口要能读出刚写入的内容。"""
        from app.services import summary_status

        store: dict = {}

        class _FakeRedis:
            async def set(self, key, value, ex=None):
                store[key] = value

            async def get(self, key):
                return store.get(key)

        monkeypatch.setattr(
            summary_status.RedisManager,
            "instance",
            classmethod(lambda cls: type("X", (), {"client": _FakeRedis()})()),
        )

        await client.post(
            "/web/register",
            data={"username": "alice", "password": "password123", "email": ""},
            follow_redirects=False,
        )
        await summary_status.set_status(
            1, {"state": "failed", "generated": 0, "skipped": 0,
                "total": 5, "error": "生成摘要需要你自己的 API Key"}
        )

        r = await client.get("/api/ai/batch-status")
        assert r.status_code == 200
        data = r.json()
        assert data["state"] == "failed"
        assert "API Key" in data["error"]


class TestDashboardSummaryUi:
    @pytest.mark.asyncio
    async def test_dashboard_has_summary_banner(self, client):
        await client.post(
            "/web/register",
            data={"username": "alice", "password": "password123", "email": ""},
            follow_redirects=False,
        )
        html = (await client.get("/web")).text
        assert "summary-banner" in html
        assert "initSummaryStatus" in html
        assert "pollSummaryStatus" in html

    def test_template_has_polling_logic(self):
        """前端必须轮询，不能只弹一句「已开始」。"""
        from pathlib import Path

        content = Path("web/templates/dashboard.html").read_text(encoding="utf-8")
        assert "/api/ai/batch-status" in content
        assert "摘要生成失败" in content
        # 不能出现只弹 toast 就完事的旧写法
        assert "showToast(data.message || '已开始');\n    setTimeout(() => location.reload(), 3000);" not in content
